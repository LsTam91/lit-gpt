import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple
from evaluate import load

import lightning as L
import torch
from lightning.fabric.loggers import CSVLogger, TensorBoardLogger
from lightning.fabric.plugins import BitsandbytesPrecision
from lightning.fabric.strategies import FSDPStrategy

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from generate.base import generate, generate_from_logits
from lit_gpt.lora import GPT, Block, Config, lora_filter, mark_only_lora_as_trainable
from lit_gpt.speed_monitor import SpeedMonitorFabric as SpeedMonitor
from lit_gpt.speed_monitor import estimate_flops, measure_flops
from lit_gpt.tokenizer import Tokenizer
from lit_gpt.utils import (
    check_valid_checkpoint_dir,
    chunked_cross_entropy,
    get_default_supported_precision,
    load_checkpoint,
    num_parameters,
)
from scripts.prepare_mydata import generate_prompt

eval_interval = 30 # each X step, one step is a batch
save_interval = 100
eval_iters = 300 # number of iter to do in the val our dataset around 600 so 300
eval_max_new_tokens = 512 #1024 # want it big to evaluate generation, but not too much
log_interval = 100 # log each X iters
devices = 1

# Hyperparameters
learning_rate = 1e-4
batch_size = 96
micro_batch_size = 3
gradient_accumulation_iters = batch_size // micro_batch_size
assert gradient_accumulation_iters > 0
max_iters = 12000  # 1 iter = 1 micro_batch
weight_decay = 0.01
lora_r = 8
lora_alpha = 16
lora_dropout = 0.05
lora_query = True
lora_key = False
lora_value = True
lora_projection = False
lora_mlp = False
lora_head = False
warmup_steps = 100

prompt_type = 'alpaca'

hparams = {k: v for k, v in locals().items() if isinstance(v, (int, float, str)) and not k.startswith("_")}


def setup(
    data_dir: Path = Path("data/alpaca"),
    checkpoint_dir: Path = Path("checkpoints/stabilityai/stablelm-base-alpha-3b"),
    out_dir: Path = Path("out/lora/alpaca"),
    precision: Optional[str] = None,
    quantize: Optional[Literal["bnb.nf4", "bnb.nf4-dq", "bnb.fp4", "bnb.fp4-dq", "bnb.int8-training"]] = None,
):
    precision = precision # or get_default_supported_precision(training=True)


    plugins = None
    if quantize is not None and quantize.startswith("bnb."):
        if "mixed" in precision:
            raise ValueError("Quantization and mixed precision is not supported.")
        dtype = {"16-true": torch.float16, "bf16-true": torch.bfloat16, "32-true": torch.float32}[precision]
        plugins = BitsandbytesPrecision(quantize[4:], dtype)
        # precision = None

    if devices > 1:
        if quantize:
            raise NotImplementedError(
                "Quantization is currently not supported for multi-GPU training. Please set devices=1 when using the"
                " --quantization flag."
            )
        strategy = FSDPStrategy(
            auto_wrap_policy={Block},
            activation_checkpointing_policy={Block},
            state_dict_type="full",
            limit_all_gathers=True,
            cpu_offload=False,
        )
    else:
        strategy = "auto"

    # tb_logger = TensorBoardLogger(root_dir="logs/tensorboard", flush_logs_every_n_steps=log_interval)
    csv_logger = CSVLogger(root_dir="logs/csv", flush_logs_every_n_steps=log_interval)
    # [tb_logger, csv_logger]
    fabric = L.Fabric(devices=devices, strategy=strategy, precision=precision, loggers=csv_logger, plugins=plugins)
    fabric.print(hparams)
    fabric.launch(main, data_dir, checkpoint_dir, out_dir, quantize)


def main(fabric: L.Fabric, data_dir: Path, checkpoint_dir: Path, out_dir: Path, quantize: Optional[str] = None):
    check_valid_checkpoint_dir(checkpoint_dir)

    speed_monitor = SpeedMonitor(fabric, window_size=50, time_unit="seconds")

    fabric.seed_everything(1337)  # same seed for every process to init model (FSDP)

    if fabric.global_rank == 0:
        os.makedirs(out_dir, exist_ok=True)

    train_data = torch.load(data_dir / "train.pt")
    val_data = torch.load(data_dir / "val.pt")

    if not any((lora_query, lora_key, lora_value, lora_projection, lora_mlp, lora_head)):
        fabric.print("Warning: all LoRA layers are disabled!")
    config = Config.from_name(
        name=checkpoint_dir.name,
        r=lora_r,
        alpha=lora_alpha,
        dropout=lora_dropout,
        to_query=lora_query,
        to_key=lora_key,
        to_value=lora_value,
        to_projection=lora_projection,
        to_mlp=lora_mlp,
        to_head=lora_head,
    )
    checkpoint_path = checkpoint_dir / "lit_model.pth"
    fabric.print(f"Loading model {str(checkpoint_path)!r} with {config.__dict__}")
    with fabric.init_module(empty_init=(devices > 1)):
        model = GPT(config)
    mark_only_lora_as_trainable(model)

    fabric.print(f"Number of trainable parameters: {num_parameters(model, requires_grad=True):,}")
    fabric.print(f"Number of non trainable parameters: {num_parameters(model, requires_grad=False):,}")

    model = fabric.setup_module(model)

    trainable_params = [p for p in model.parameters() if p.requires_grad]
    if isinstance(fabric.strategy.precision, BitsandbytesPrecision):
        import bitsandbytes as bnb

        optimizer = bnb.optim.PagedAdamW(trainable_params, lr=learning_rate, weight_decay=weight_decay)
    else:
        optimizer = torch.optim.AdamW(trainable_params, lr=learning_rate, weight_decay=weight_decay)
    optimizer = fabric.setup_optimizers(optimizer)

    # strict=False because missing keys due to LoRA weights not contained in state dict
    load_checkpoint(fabric, model, checkpoint_path, strict=False)

    fabric.seed_everything(1337 + fabric.global_rank)

    train_time = time.perf_counter()
    train(fabric, model, optimizer, train_data, val_data, checkpoint_dir, out_dir, speed_monitor)
    fabric.print(f"Training time: {(time.perf_counter()-train_time):.2f}s")
    if fabric.device.type == "cuda":
        fabric.print(f"Memory used: {torch.cuda.max_memory_allocated() / 1e9:.02f} GB")

    # Save the final LoRA checkpoint at the end of training
    save_path = out_dir / "lit_model_lora_finetuned.pth"
    save_lora_checkpoint(fabric, model, save_path)


def train(
    fabric: L.Fabric,
    model: GPT,
    optimizer: torch.optim.Optimizer,
    train_data: List[Dict],
    val_data: List[Dict],
    checkpoint_dir: Path,
    out_dir: Path,
    speed_monitor: SpeedMonitor,
) -> None:
    tokenizer = Tokenizer(checkpoint_dir)
    longest_seq_length, longest_seq_ix = get_longest_seq_length(train_data)
    model.max_seq_length = longest_seq_length
    fabric.print(
        f"The longest sequence length in the train data is {longest_seq_length}, the model's maximum sequence length is"
        f" {model.max_seq_length} and context length is {model.config.block_size}"
    )

    validate(fabric, model, val_data, tokenizer)  # sanity check

    with torch.device("meta"):
        meta_model = GPT(model.config)
        mark_only_lora_as_trainable(meta_model)
        # "estimated" is not as precise as "measured". Estimated is optimistic but widely used in the wild.
        # When comparing MFU or FLOP numbers with other projects that use estimated FLOPs,
        # consider passing `SpeedMonitor(flops_per_batch=estimated_flops)` instead
        estimated_flops = estimate_flops(meta_model) * micro_batch_size
        fabric.print(f"Estimated TFLOPs: {estimated_flops * fabric.world_size / 1e12:.2f}")
        # this assumes that all samples have a fixed length equal to the longest sequence length
        # which is most likely false during finetuning
        x = torch.randint(0, 1, (micro_batch_size, longest_seq_length))
        measured_flops = measure_flops(meta_model, x)
        fabric.print(f"Measured TFLOPs: {measured_flops * fabric.world_size / 1e12:.2f}")
        del meta_model, x

    step_count = 0
    total_lengths = 0
    total_t0 = time.perf_counter()

    for iter_num in range(max_iters):
        if step_count <= warmup_steps:
            # linear warmup
            lr = learning_rate * step_count / warmup_steps
            for param_group in optimizer.param_groups:
                param_group["lr"] = lr

        iter_t0 = time.perf_counter()

        input_ids, targets = get_batch(fabric, train_data, longest_seq_ix if iter_num == 0 else None)

        is_accumulating = (iter_num + 1) % gradient_accumulation_iters != 0
        with fabric.no_backward_sync(model, enabled=is_accumulating):
            logits = model(input_ids, lm_head_chunk_size=128)
            # shift the targets such that output n predicts token n+1
            logits[-1] = logits[-1][..., :-1, :]
            loss = chunked_cross_entropy(logits, targets[..., 1:])
            fabric.backward(loss / gradient_accumulation_iters)

        if not is_accumulating:
            optimizer.step()
            optimizer.zero_grad()
            step_count += 1

        t1 = time.perf_counter()
        total_lengths += input_ids.size(1)
        speed_monitor.on_train_batch_end(
            (iter_num + 1) * micro_batch_size,
            t1 - total_t0,
            # this assumes that device FLOPs are the same and that all devices have the same batch size
            fabric.world_size,
            flops_per_batch=measured_flops,
            lengths=total_lengths,
        )
        if iter_num % log_interval == 0:
            fabric.print(
                f"iter {iter_num} step {step_count}: loss {loss.item():.4f}, iter time:"
                f" {(t1 - iter_t0) * 1000:.2f}ms{' (optimizer.step)' if not is_accumulating else ''}"
            )

        if not is_accumulating and step_count % eval_interval == 0:
            t0 = time.perf_counter()
            val_loss = validate(fabric, model, val_data, tokenizer)
            t1 = time.perf_counter() - t0
            speed_monitor.eval_end(t1)
            fabric.print(f"step {iter_num}: val loss {val_loss.item():.4f}, val time: {t1 * 1000:.2f}ms")
            fabric.barrier()
        if not is_accumulating and step_count % save_interval == 0:
            checkpoint_path = out_dir / f"iter-{iter_num:06d}-ckpt.pth"
            save_lora_checkpoint(fabric, model, checkpoint_path)


@torch.inference_mode()
def validate(fabric: L.Fabric, model: GPT, val_data: List[Dict], tokenizer: Tokenizer) -> torch.Tensor:
    fabric.print("Validating ...")
    model.eval()
    losses = torch.zeros(eval_iters)
    for k in range(eval_iters):
        input_ids, targets = get_batch(fabric, val_data)
        logits = model(input_ids)
        losses[k] = chunked_cross_entropy(logits[..., :-1, :], targets[..., 1:], chunk_size=0)
    val_loss = losses.mean()

    # produce an example:
    instruction = "Réponds clairement à la question en te basant exclusivement sur le paragraphe associé"
    inn = "Napoléon est arrivé au pouvoir en peu d'années. Une révolution l'a enfanté, un peuple l'a choisi, un pape l'a couronné. Il a agrandi les frontières de son Empire, comme Charlemagne et Louis XIV, et construit son État au centre de l'Europe."
    fabric.print(instruction)
    sample = {"instruction": instruction, "input": inn}
    prompt = generate_prompt(sample, prompt_type)
    encoded = tokenizer.encode(prompt, device=fabric.device)
    with fabric.init_tensor():
        # do not set `max_seq_length=max_returned_token` because memory is not a concern here
        model.set_kv_cache(batch_size=1)
    output = generate(model, encoded, max_returned_tokens=len(encoded) + eval_max_new_tokens, temperature=0.8)
    model.clear_kv_cache()
    output = tokenizer.decode(output)
    fabric.print(output)

    model.train()
    return val_loss

# @torch.inference_mode()
# def validate(fabric: L.Fabric, model: GPT, val_data: List[Dict], tokenizer: Tokenizer) -> torch.Tensor:
#     fabric.print("Validating ...")
#     model.eval()
#     losses = torch.zeros(eval_iters)
#     # TODO: Mettre les métrique dans modèle pour les charge une seule fois
#     rouge_metric = load("rouge")
#     sacrebleu_metric = load("sacrebleu")
#     rouge_scores = []
#     sacrebleu_scores = []

#     for k in range(eval_iters):
#         input_ids, targets = get_batch(fabric, val_data)
#         logits = model(input_ids)
#         losses[k] = chunked_cross_entropy(logits[..., :-1, :], targets[..., 1:], chunk_size=0)

#         # print(logits.shape, targets.shape) #torch.Size([2, 1082, 32000]) torch.Size([2, 1082])

#         for u in range(logits.shape(0)):
#             y = generate_from_logits(input_ids[u], logits[u])
#             output = tokenizer.decode(y)
#             answer_split = output.split("### Response:")

#             mask = targets[u] != -1
#             # npad = sum(mask.tolist())
#             filtered_target = torch.masked_select(targets[u], mask)
#             filtered_target = tokenizer.decode(filtered_target)
#             target_split = output.split("### Response:")

#             # compute rouge score
#             rouge_score = rouge_metric.compute(predictions=[answer_split[1]], references=[target_split[1]])
#             rouge_scores.append(rouge_score)

#             # Compute SacreBLEU scores
#             sacrebleu_score = sacrebleu_metric.compute(predictions=[answer_split[1]], references=[target_split[1]])
#             sacrebleu_scores.append(sacrebleu_score)

#         # # print([token for token in targets[0, 1:] if token != -1], logits[..., :-1, :].argmax(dim=-1)[0] )
#         # # Compute Rouge scores
#         # # fonctionne une fois puis "IndexError: list index out of range" sans doute car "### Response:" pas ds logits
#         # generated_text = [tokenizer.decode(logits[..., :-1, :].argmax(dim=-1)[i]).split("### Response:")[1].strip() for i in range(logits.shape[0])]

#         # reference_text = [tokenizer.decode(torch.tensor([token for token in targets[i, 1:] if token != -1])).split("### Response:")[1].strip() for i in range(targets.shape[0])]
#         # print(generated_text, reference_text)

#         # rouge_score = rouge_metric.compute(predictions=generated_text, references=reference_text)
#         # rouge_scores.append(rouge_score)

#         # # Compute SacreBLEU scores
#         # sacrebleu_score = sacrebleu_metric.compute(predictions=generated_text, references=reference_text)
#         # sacrebleu_scores.append(sacrebleu_score)

#     val_loss = losses.mean()
#     rouge_f1 = sum(score['rougeL'] for score in rouge_scores) / len(rouge_scores)
#     sacrebleu_score = sum(score['score'] for score in sacrebleu_scores) / len(sacrebleu_scores)

#     # Print the metrics
#     fabric.print(f"Validation Loss: {val_loss}")
#     fabric.print(f"Rouge F1 Score: {rouge_f1}")
#     fabric.print(f"SacreBLEU Score: {sacrebleu_score}")

#     values = {"loss": val_loss, "rouge": rouge_f1, "sacrebleu": sacrebleu_score}
#     fabric.log_dict(values)

#     model.train()
#     return val_loss



def get_batch(
    fabric: L.Fabric, data: List[Dict], longest_seq_ix: Optional[int] = None
) -> Tuple[torch.Tensor, torch.Tensor]:
    ix = torch.randint(len(data), (micro_batch_size,))
    if longest_seq_ix is not None:
        # force the longest sample at the beginning so potential OOMs happen right away
        ix[0] = longest_seq_ix

    input_ids = [data[i]["input_ids"].type(torch.int64) for i in ix]
    labels = [data[i]["labels"].type(torch.int64) for i in ix]

    # this could be `longest_seq_length` to have a fixed size for all batches
    max_len = max(len(s) for s in input_ids)

    def pad_right(x, pad_id):
        # pad right based on the longest sequence
        n = max_len - len(x)
        return torch.cat((x, torch.full((n,), pad_id, dtype=x.dtype)))

    x = torch.stack([pad_right(x, pad_id=0) for x in input_ids])
    y = torch.stack([pad_right(x, pad_id=-1) for x in labels])

    if fabric.device.type == "cuda" and x.device.type == "cpu":
        x, y = fabric.to_device((x.pin_memory(), y.pin_memory()))
    else:
        x, y = fabric.to_device((x, y))
    return x, y


def get_longest_seq_length(data: List[Dict]) -> Tuple[int, int]:
    # find out the minimum max_seq_length required during fine-tuning (saves memory!)
    lengths = [len(d["input_ids"]) for d in data]
    longest_seq_length = max(lengths)
    longest_seq_ix = lengths.index(longest_seq_length)
    return longest_seq_length, longest_seq_ix


def save_lora_checkpoint(fabric, model, file_path: Path):
    fabric.print(f"Saving LoRA weights to {str(file_path)!r}")
    fabric.save(file_path, {"model": model}, filter={"model": lora_filter})


if __name__ == "__main__":
    # Uncomment this line if you see an error: "Expected is_sm80 to be true, but got false"
    # torch.backends.cuda.enable_flash_sdp(False)
    torch.set_float32_matmul_precision("high")

    from jsonargparse import CLI

    CLI(setup)
