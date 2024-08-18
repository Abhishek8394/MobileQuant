# original implementation
# we still need to run smoothquant as omniquant initializes the weight from it
CUDA_VISIBLE_DEVICES=0 python ptq/smoothquant.py --hf_path checkpoints/baselines/gemma-2b --original_omniquant --alpha 0.5
CUDA_VISIBLE_DEVICES=0 python ptq/generate_act_range.py --hf_path checkpoints/baselines/gemma-2b


EPOCHS=20
NSAMPLES=128
CKPT=checkpoints/baselines/gemma-2b
OUTPUT_DIR=results/gemma-2b-omniquant-orig
BATCH_SIZE=1
LET_LR=5e-3
LET_MIN_LR=5e-3
LWC_LR=1e-2
LWC_MIN_LR=5e-3


CUDA_VISIBLE_DEVICES=0 python ptq/mobilequant.py --lwc --let --tasks wikitext \
  --act_bitwidth 8 --epochs ${EPOCHS} --nsamples ${NSAMPLES} --dtype float32 \
  --deactive_amp --hf_path ${CKPT} --output_dir ${OUTPUT_DIR} \
  --let_lr ${LET_LR} --let_min_lr ${LET_MIN_LR} \
  --lwc_lr ${LWC_LR} --lwc_min_lr ${LWC_MIN_LR} \
  --batch_size ${BATCH_SIZE} --weight_bitwidth 8  --original_omniquant


CUDA_VISIBLE_DEVICES=0 python eval/harness_eval.py --tasks wikitext \
    --mode custom --hf_path ${OUTPUT_DIR}