import numpy as np
import os.path as osp
from tqdm import tqdm
from pathlib import Path
from glob import glob
import onnx, onnxruntime
from copy import deepcopy
from functools import partial
from datasets import load_dataset
import shutil, subprocess, re, zlib, subprocess
import torch, os, argparse, json, logging, sys, gc, math
import torch.nn as nn
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig


from mobilellm.model.sim_model import SimConfig, SimModel, create_conv_model, Sim_Head, Sim_Body
from mobilellm.utils.bench import print_model_size
from mobilellm.utils.io import json_load, json_save
from mobilellm.quantization.qmodule import QLinear, QRMSNorm, QLayerNorm, QMatMul, QSiLU, QGELU, Quantizer
from device.utils import incorporate_l2norm, update_qcfg_sim, to_device, dump_onnx_and_encoding
from device.utils import disable_quant_sim, update_encodings


from aimet_torch.model_preparer import prepare_model
from aimet_torch.qc_quantize_op import QcQuantizeWrapper
from aimet_torch.quantsim import QuantizationSimModel, load_encodings_to_sim
from aimet_common.defs import QuantScheme, QuantizationDataType
from aimet_common.utils import AimetLogger
AimetLogger.set_level_for_all_areas(logging.INFO)


from mobilellm.model.hf_config import HFConfig
from mobilellm.model.hf_model import HFForCausalLM
AutoConfig.register("hfmodel", HFConfig)
AutoModelForCausalLM.register(HFConfig, HFForCausalLM)


parser = argparse.ArgumentParser()

parser.add_argument('--hf_path', type=str, default=None, help='path of the hf model')
parser.add_argument('--model_name', type=str, default=None)
parser.add_argument('--max_length', type=int, default=2048, help='max seq len for the samples')
parser.add_argument('--calib_data', type=str, default='pileval', help='the calibration data')
parser.add_argument('--calib_path', type=str, default='data/pile/val.jsonl.zst', help='the calibration data')
parser.add_argument('--num_calib_samples', type=int, default=512, help='num of calibration samples')
parser.add_argument("--output_dir", default='results/sim_{}_debug', type=str)
parser.add_argument('--default_config', type=str, default='assets/aimet_config.json', help='the default config file')
parser.add_argument('--use_rand_samples', default=False, action="store_true")
parser.add_argument('--use_conv', default=False, action="store_true")
parser.add_argument('--per_channel', default=False, action="store_true")
parser.add_argument('--num_blocks', type=int, default=None)
parser.add_argument('--kv_cache_bitwidth', type=int, default=8)
parser.add_argument('--weight_bitwidth', type=int, default=8)
parser.add_argument('--act_bitwidth', type=int, default=8)
parser.add_argument("--act_dict_path", default=None, type=str)


args = parser.parse_args()
assert(args.hf_path is not None)
if args.hf_path.endswith('/'):
    args.hf_path = args.hf_path[:-1]
args.model_name = osp.basename(args.hf_path)
args.output_dir = args.output_dir.format(args.model_name)
args.model_path = osp.join(args.hf_path, f"sim_{args.model_name}.pth")


if args.per_channel:
    args.default_config = "assets/aimet_per_channel_config.json"


args.act_dict_path = osp.join(args.hf_path, "act_dict.json")
args.override_qcfg_path = osp.join(args.hf_path, "default_qcfg.json")


seed = 1337
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn



class HF_Body(nn.Module):
    def __init__(self, layers) -> None:
        super().__init__()
        self.layers = layers

    def forward(self, x: torch.Tensor, attention_mask: torch.Tensor, position_ids: torch.Tensor):
        for i, block in enumerate(self.layers):
            x = self.layers[i](x, attention_mask, position_ids)[0]
        return x


def to_hf(x, device):
    return x.to(device).unsqueeze(0) if isinstance(x, torch.Tensor) else tuple(to_device(y, device) for y in x)


# def disable_quant_hf(model):
#     for name, module in reversed(model._modules.items()):
#         if isinstance(module, QLinear):
#             pass
#             # # if not ("q_proj" in name or "k_proj" in name or "v_proj" in name or "o_proj" in name or "w1" in name or "w3" in name):
#             # if not ("q_proj" in name or "k_proj" in name or "v_proj" in name or "w1" in name or "w3" in name or "o_proj" in name or "w2" in name):
#             #     if model._modules[name].input_quantizer is not None:
#             #         model._modules[name].input_quantizer.enable = False
#             #     if model._modules[name].weight_quantizer is not None:
#             #         model._modules[name].weight_quantizer.enable = False
#             #     if model._modules[name].output_quantizer is not None:
#             #         model._modules[name].output_quantizer.enable = False
#         elif isinstance(module, (QRMSNorm, QLayerNorm)):
#             # if model._modules[name].input_quantizer is not None:
#             #     model._modules[name].input_quantizer.enable = False
#             # if model._modules[name].weight_quantizer is not None:
#             #     model._modules[name].weight_quantizer.enable = False
#             # if model._modules[name].output_quantizer is not None:
#             #     model._modules[name].output_quantizer.enable = False
#             pass
#         elif isinstance(module, QMatMul):
#             # if not ("qk_bmm" in name or "pv_bmm" in name):
#             # # if not ("pv_bmm" in name):
#             #     if model._modules[name].input_quantizer is not None:
#             #         model._modules[name].input_quantizer.enable = False
#             #     if model._modules[name].input2_quantizer is not None:
#             #         model._modules[name].input2_quantizer.enable = False
#             #     if model._modules[name].output_quantizer is not None:
#             #         model._modules[name].output_quantizer.enable = False
#             pass
#         elif len(list(module.children())) > 1:
#             disable_quant_hf(module)
#     return model


def disable_quant_sim(sim_model):
    for name, module in sim_model.model.named_modules():
        if isinstance(module, QcQuantizeWrapper):
            # for i in range(len(module.input_quantizers)):
            #     module.input_quantizers[i].enabled = False
            # for i in range(len(module.output_quantizers)):
            #     module.output_quantizers[i].enabled = False
            # for i in list(module.param_quantizers.keys()):
            #     module.param_quantizers[i].enabled = False
            for i in range(len(module.input_quantizers)):
                module.input_quantizers[i].enabled = True
            # if name.startswith('module_matmul'):
            #     if name == "module_matmul":
            #         for i in range(len(module.input_quantizers)):
            #             module.input_quantizers[i].enabled = False
            #         for i in range(len(module.output_quantizers)):
            #             module.output_quantizers[i].enabled = False
            #     else:
            #         ind = name.split('_')[-1]
            #         assert ind.isdigit()
            #         ind = int(ind)
            #         if ind % 2 == 0:
            #             for i in range(len(module.input_quantizers)):
            #                 module.input_quantizers[i].enabled = False
            #             for i in range(len(module.output_quantizers)):
            #                 module.output_quantizers[i].enabled = False
            if not (name.startswith('module_matmul') or name.startswith("module_add") or "norm.module_mul" in name or "module_normalize" in name or "q_proj" in name or "k_proj" in name or "v_proj" in name or "w1" in name or "w3" in name or "o_proj" in name or "w2" in name):
                for i in range(len(module.input_quantizers)):
                    module.input_quantizers[i].enabled = False
                for i in range(len(module.output_quantizers)):
                    module.output_quantizers[i].enabled = False
                for i in list(module.param_quantizers.keys()):
                    module.param_quantizers[i].enabled = False
            
    return sim_model



def main():
    #####################################################################
    # tokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.hf_path, use_fast=False, legacy=False, trust_remote_code=True)
    #####################################################################
    # HF model
    hf_config = AutoConfig.from_pretrained(args.hf_path, trust_remote_code=True)
    hf_config.use_matmul_as_module = True
    hf_config._attn_implementation = "eager"
    hf_config.l2norm_as_rmsnorm = True
    model_hf = AutoModelForCausalLM.from_pretrained(args.hf_path, config=hf_config, device_map='auto', torch_dtype=torch.float32, low_cpu_mem_usage=True, trust_remote_code=True, attn_implementation="eager")
    from mobilellm.quantization.qmodule import QuantConfig, create_sim_qmodel, set_scale_and_offset, update_qcfg
    model_hf = create_sim_qmodel(model_hf)
    override_qcfg = json_load(args.override_qcfg_path)
    model_hf = update_qcfg(model_hf, override_qcfg)
    act_dict = json_load(args.act_dict_path)
    model_hf = set_scale_and_offset(model_hf, act_dict, 'parameter') 
    #####################################################################

    #####################################################################
    # sim model
    config = SimConfig.from_name(args.model_name)
    config.block_size = args.max_length
    model_ori = SimModel(config)
    for x in model_ori.parameters(): 
        x.requires_grad = False
    ckpt = torch.load(args.model_path, map_location='cpu')
    msg = model_ori.load_state_dict(ckpt, strict=True)
    print(msg)
    if args.use_conv:
        model_ori = create_conv_model(model_ori)


    #####################################################################
    # calib data
    if args.calib_data == 'pileval':
        dataset = load_dataset("json", data_files=args.calib_path, split="train")
    elif args.calib_data == 'wikitext':
        dataset = load_dataset('wikitext', 'wikitext-103-raw-v1', split="train")
    else:
        raise NotImplementedError
    dataset = dataset.shuffle(seed=seed+1)
    samples = []
    position_ids = torch.arange(0, args.max_length, dtype=torch.long)
    for i in tqdm(range(args.num_calib_samples)):
        if "text" in dataset[i]:        line = dataset[i]["text"]
        elif "content" in dataset[i]:   line = dataset[i]["content"]
        elif "ctx" in dataset[i]:       line = dataset[i]["ctx"]
        else:                           raise NotImplementedError
        input_ids = tokenizer(line.strip(), return_tensors="pt", max_length=args.max_length, truncation=True).input_ids[0]
        valid_len = input_ids.shape[0]
        attention_mask = SimModel._make_causal_mask(args.max_length, args.max_length, args.max_length)
        attention_mask = config.neg_inf * attention_mask
        input_ids = torch.nn.functional.pad(input_ids, (0, args.max_length - input_ids.shape[0]), value=tokenizer.eos_token_id)
        samples.append((input_ids, attention_mask, position_ids))
        if args.use_rand_samples:
            rand_ids       = torch.randint(tokenizer.bos_token_id+1, tokenizer.vocab_size-1, size=(args.max_length,), dtype=torch.int32)
            attention_mask = SimModel._make_causal_mask(args.max_length, args.max_length, args.max_length)
            attention_mask = config.neg_inf * attention_mask
            samples.append((rand_ids, attention_mask, position_ids))

    
    #####################################################################
    # sub modules
    if args.num_blocks is None:
        args.num_blocks = config.n_layer
    model_head = Sim_Head.from_sim(model_ori).cuda()
    model_body = Sim_Body.from_sim(model_ori, args.num_blocks).cuda()
    device = next(model_body.parameters()).device
    model_head.eval()
    model_body.eval()
    del model_ori
    gc.collect()
    torch.cuda.empty_cache()

    model_hf = HF_Body(model_hf.model.layers[:args.num_blocks])

    #####################################################################
    # sample input and output
    with torch.no_grad():
        ctx_sample = model_head(*to_device(samples[0], device))

    #####################################################################
    # prepare model
    model_ctx = prepare_model(model_body, concrete_args={'k_cache': None, 'v_cache': None})
    os.makedirs(args.output_dir, exist_ok=True)
    ctx_dir = osp.join(args.output_dir, 'ctx')
    os.makedirs(ctx_dir, exist_ok=True)
    
    #####################################################################
    ## Sim model
    sim_ctx = QuantizationSimModel(model=model_ctx, quant_scheme=QuantScheme.post_training_tf, dummy_input=ctx_sample, rounding_mode='nearest', in_place=True, config_file=args.default_config, default_output_bw=args.act_bitwidth, default_param_bw=args.weight_bitwidth, default_data_type=QuantizationDataType.int)
    #####################################################################
    ## 16-bit
    sixteen_bit_output_activations = ['module_normalize', 'o_proj', 'w2', 'lm_head', 'softmax']
    sixteen_bit_input_activations = ['module_normalize', 'norm.module_mul', 'w2', 'lm_head', 'softmax']
    sim_ctx = update_qcfg_sim(sim_ctx, sixteen_bit_input_activations, sixteen_bit_output_activations)

    #####################################################################
    ## Pre-calibration
    @torch.no_grad()
    def pass_ctx_calibration_data(sim_model, *inargs):
        sim_model.eval()
        with torch.no_grad():
            for i in tqdm(range(len(samples))):
                input4 = model_head(*to_device(samples[i], device))
                sim_model(*input4)
    onnx_ctx_path = osp.join(ctx_dir, 'model_ctx.onnx')
    sim_ctx.compute_encodings(forward_pass_callback=pass_ctx_calibration_data, forward_pass_callback_args=None)
    print('Exporting the ctx onnx/encodings')
    dump_onnx_and_encoding(sim_ctx, ctx_sample, onnx_ctx_path, input_names=['input_feats', 'attention_mask', 'cos', 'sin'])
    sim_ctx.model = sim_ctx.model.to(device)

    #####################################################################
    ## Override
    # Transfer the encodings of the ctx model to the gen model
    ctx_encodings = json_load(onnx_ctx_path.replace('.onnx', '_torch.encodings'))
    # override the encoding of sim_ctx
    act_dict = json_load(args.act_dict_path)
    ctx_encodings = update_encodings(ctx_encodings, act_dict, args.num_blocks, 1.0/math.sqrt(config.head_dim))
    json_save(onnx_ctx_path.replace('.onnx', '_torch_overrided.encodings'), ctx_encodings)
    load_encodings_to_sim(sim_ctx, onnx_ctx_path.replace('.onnx', '_torch_overrided.encodings'))

    #####################################################################
    ## Layer-by-layer
    # for i in range(len(model_hf.layers)):
    #     model_hf.layers[i] = disable_quant_hf(model_hf.layers[i])
    sim_ctx = disable_quant_sim(sim_ctx)

    with torch.no_grad():
        hf_output = model_hf(ctx_sample[0].unsqueeze(0), attention_mask=attention_mask.unsqueeze(0).cuda(), position_ids=position_ids.unsqueeze(0).cuda())[0].cpu().data.numpy()
        fp_output = sim_ctx.model(*to_device(ctx_sample, device))[0].cpu().data.numpy()

    print("Comparing HF and FP")
    try:
        np.testing.assert_allclose(hf_output, fp_output, rtol=1e-02, atol=1e-03)
    except AssertionError as e:
        print(e)

    
    # # sim_gen = update_qcfg_sim(sim_gen, sixteen_bit_input_activations, sixteen_bit_output_activations)
    # #####################################################################
    # ## Calibration
    # #####################################################################
    # 
    # onnx_gen_path = osp.join(gen_dir, 'model_gen.onnx')
    # #####################################################################
    
    # @torch.no_grad()
    # def pass_gen_calibration_data(sim_model, *inargs):
    #     sim_model.eval()
    #     with torch.no_grad():
    #         sim_model(*gen_sample)
    # #####################################################################
    # 
    # # sim_gen.compute_encodings(forward_pass_callback=pass_gen_calibration_data, forward_pass_callback_args=None)
    

    

    

    # sim_output = sim_ctx.model(*to_device(ctx_sample, device))[0].cpu().data.numpy()
    # print("Comparing HF and Sim")
    # try:
    #     np.testing.assert_allclose(fp_output, sim_output, rtol=1e-01, atol=1e-03)
    # except AssertionError as e:
    #     print(e)

    # # del samples, dataset
    # # gc.collect()
    # # torch.cuda.empty_cache()
    # ###########################################################################################
    # # del sim_ctx
    # # gc.collect()
    # # torch.cuda.empty_cache() 
    # # #####################################################################
    # # print('Exporting the gen onnx/encodings')
    # # dump_onnx_and_encoding(sim_gen, gen_sample, onnx_gen_path, input_names=['input_feats', 'attention_mask', 'cos', 'sin', 'k_cache', 'v_cache'])
    # # #####################################################################
    # # # Transfer the encodings of the ctx model to the gen model
    # # ctx_encodings = json_load(onnx_ctx_path.replace('.onnx', '_torch.encodings'))
    # # gen_encodings = json_load(onnx_gen_path.replace('.onnx', '_torch.encodings'))

    # # if args.act_dict_path is not None:
    # #     # override the encoding of sim_ctx
    # #     act_dict = json_load(args.act_dict_path)
    # #     ctx_encodings = update_encodings(ctx_encodings, act_dict, args.num_blocks, 1.0/math.sqrt(config.head_dim))
    # #     json_save(onnx_ctx_path.replace('.onnx', '_torch.encodings'), ctx_encodings)

    # # # Compute the k/v cache encodings
    # # k_range, v_range = [], []
    # # for i in range(args.num_blocks):
    # #     if args.use_conv:
    # #         k_enc = ctx_encodings['activation_encodings']["layers.{}.self_attn.k_proj.conv".format(i)]['output']['0']
    # #         v_enc = ctx_encodings['activation_encodings']["layers.{}.self_attn.v_proj.conv".format(i)]['output']['0']
    # #     else:
    # #         k_enc = ctx_encodings['activation_encodings']["layers.{}.self_attn.k_proj".format(i)]['output']['0']
    # #         v_enc = ctx_encodings['activation_encodings']["layers.{}.self_attn.v_proj".format(i)]['output']['0']
    # #     k_range.append([k_enc['min'], k_enc['max']])
    # #     v_range.append([v_enc['min'], v_enc['max']])
    # # k_range, v_range = torch.tensor(k_range), torch.tensor(v_range)
    # # k_min, k_max, v_min, v_max = torch.min(k_range[:,0]).item(), torch.max(k_range[:,1]).item(), torch.min(v_range[:,0]).item(), torch.max(v_range[:,1]).item()

    # # qmax = 2 ** args.kv_cache_bitwidth - 1
    # # k_scale, v_scale = (k_max-k_min)/qmax, (v_max-v_min)/qmax
    # # k_cache_enc = { "bitwidth": args.kv_cache_bitwidth, "dtype": "int", "is_symmetric": "False", "max": k_max, "min": k_min, "offset": int(k_min/k_scale), "scale": k_scale}
    # # v_cache_enc = { "bitwidth": args.kv_cache_bitwidth, "dtype": "int", "is_symmetric": "False", "max": v_max, "min": v_min, "offset": int(v_min/v_scale), "scale": v_scale}
    # # json_save(onnx_gen_path.replace('.onnx', '_kv_cache.encodings'), {'k_cache': k_cache_enc, 'v_cache': v_cache_enc})

    # # # # Transfer the parameter encodings
    # # # for k in list(gen_encodings['param_encodings'].keys()):
    # # #     assert(len(gen_encodings['param_encodings'][k]) == len(ctx_encodings['param_encodings'][k]))
    # # #     gen_encodings['param_encodings'][k] = ctx_encodings['param_encodings'][k]

    # # ###############################################################################################################################################
    # # # TODO: should be done in a more systematic way
    # # # Transfer the activation encodings (hard coding)
    # # # ctx and gen only differ in some of the concat layers
    # # ctx_missing = []
    # # for k in list(ctx_encodings['activation_encodings'].keys()):
    # #     assert (('module_cat' in k) or (k in gen_encodings['activation_encodings']))
    # #     if len(k) > len('module_cat') and k[:len('module_cat')] == 'module_cat':
    # #         ctx_missing.append(k)

    # # gen_missing = []
    # # for k in list(gen_encodings['activation_encodings'].keys()):
    # #     if len(k) > len('module_cat') and k[:len('module_cat')] == 'module_cat':
    # #         gen_missing.append(k)
    # #     elif k in ctx_encodings['activation_encodings']:
    # #         assert(len(gen_encodings['activation_encodings'][k]) == len(ctx_encodings['activation_encodings'][k]))
    # #         gen_encodings['activation_encodings'][k] = ctx_encodings['activation_encodings'][k]
    # #     else:
    # #         assert('layers' in k and 'module_cat' in k)
    # #         op_ind, layer_ind = int(k.split('_')[-1]), int(k.split('.')[1])
    # #         matmul_ind = 2 * layer_ind + op_ind % 2
    # #         if op_ind % 2 == 0:
    # #             # use the encoding from qk_bmm, be careful about the input order of qk_bmm
    # #             if matmul_ind == 0:
    # #                 enc = ctx_encodings['activation_encodings']['module_matmul']['input']['1']
    # #             else:
    # #                 enc = ctx_encodings['activation_encodings']['module_matmul_{}'.format(matmul_ind)]['input']['1']
    # #             gen_encodings['activation_encodings'][k]['input']['0']  = enc
    # #             gen_encodings['activation_encodings'][k]['input']['1']  = enc
    # #             gen_encodings['activation_encodings'][k]['output']['0'] = enc
    # #         else:
    # #             # use the encoding from pv_bmm
    # #             enc = ctx_encodings['activation_encodings']['module_matmul_{}'.format(matmul_ind)]['input']['1']
    # #             gen_encodings['activation_encodings'][k]['input']['0']  = enc
    # #             gen_encodings['activation_encodings'][k]['input']['1']  = enc
    # #             gen_encodings['activation_encodings'][k]['output']['0'] = enc

    # # # handle the missing encodings, which are the concat layers in rope
    # # gen_missing = sorted(gen_missing, key=lambda x: int(x.split('_')[-1]))
    # # ctx_missing = sorted(ctx_missing, key=lambda x: int(x.split('_')[-1]))
    # # assert(len(gen_missing) == len(ctx_missing))

    # # for u, v in zip(gen_missing, ctx_missing):
    # #     assert(len(gen_encodings['activation_encodings'][u]) == len(ctx_encodings['activation_encodings'][v]))
    # #     gen_encodings['activation_encodings'][u] = ctx_encodings['activation_encodings'][v]
    
    # # json_save(onnx_gen_path.replace('.onnx', '_transfered.encodings'), gen_encodings)
    

if __name__ == '__main__':
    main()