# Copyright © 2024 Apple Inc.

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from mlx.utils import tree_flatten


def grad_checkpoint(layer):
    """
    Update all instances of type(layer) to use gradient checkpointing.
    """
    fn = type(layer).__call__

    def checkpointed_fn(model, *args, **kwargs):
        def inner_fn(params, *args, **kwargs):
            model.update(params)
            return fn(model, *args, **kwargs)

        return mx.checkpoint(inner_fn)(model.trainable_parameters(), *args, **kwargs)

    type(layer).__call__ = checkpointed_fn


@dataclass
class TrainingArgs:
    batch_size: int = field(default=4, metadata={"help": "Minibatch size."})
    iters: int = field(default=100, metadata={"help": "Iterations to train for."})
    val_batches: int = field(
        default=25,
        metadata={
            "help": "Number of validation batches, -1 uses the entire validation set."
        },
    )
    steps_per_report: int = field(
        default=10,
        metadata={"help": "Number of training steps between loss reporting."},
    )
    steps_per_eval: int = field(
        default=200, metadata={"help": "Number of training steps between validations."}
    )
    steps_per_save: int = field(
        default=100, metadata={"help": "Save the model every number steps"}
    )
    max_seq_length: int = field(
        default=2048, metadata={"help": "Maximum sequence length."}
    )
    adapter_file: str = field(
        default="adapters.safetensors",
        metadata={"help": "Save/load path for the trained adapter weights."},
    )
    grad_checkpoint: bool = field(
        default=False,
        metadata={"help": "Use gradient checkpointing to reduce memory use."},
    )


def default_loss(model, inputs, targets, lengths):
    logits = model(inputs)
    logits = logits.astype(mx.float32)

    length_mask = mx.arange(inputs.shape[1])[None, :] < lengths[:, None]

    ce = nn.losses.cross_entropy(logits, targets) * length_mask
    ntoks = length_mask.sum()
    ce = ce.sum() / ntoks

    return ce, ntoks


def iterate_batches(dataset, tokenizer, batch_size, max_seq_length, train=False):
    # Sort by length:
    idx = sorted(range(len(dataset)), key=lambda idx: len(dataset[idx]))
    if len(dataset) < batch_size:
        raise ValueError(
            f"Dataset must have at least batch_size={batch_size}"
            f" examples but only has {len(dataset)}."
        )

    # Make the batches:
    batch_idx = [
        idx[i : i + batch_size] for i in range(0, len(idx) - batch_size + 1, batch_size)
    ]

    while True:
        indices = np.random.permutation(len(batch_idx))
        for i in indices:
            # Encode batch
            batch = [tokenizer.encode(dataset[j]) for j in batch_idx[i]]
            lengths = [len(x) for x in batch]

            if max(lengths) > max_seq_length:
                print(
                    f"[WARNING] Some sequences are longer than {max_seq_length} tokens. "
                    f"The longest sentence {max(lengths)} will be truncated to {max_seq_length}. "
                    "Consider pre-splitting your data to save memory."
                )

            # Pad to the nearest multiple of 8 or the maximum length
            pad_to = 8
            max_length_in_batch = pad_to * ((max(lengths) + pad_to - 1) // pad_to)
            max_length_in_batch = min(max_length_in_batch, max_seq_length)

            batch_arr = np.zeros((batch_size, max_length_in_batch), np.int32)

            for j in range(batch_size):
                truncated_length = min(lengths[j], max_seq_length)
                batch_arr[j, :truncated_length] = batch[j][:truncated_length]
                lengths[j] = (
                    truncated_length  # Update lengths to match truncated lengths
                )
            batch = mx.array(batch_arr)

            yield batch[:, :-1], batch[:, 1:], mx.array(lengths)

        if not train:
            break


def evaluate(
    model,
    dataset,
    tokenizer,
    batch_size,
    num_batches,
    max_seq_length=2048,
    loss: callable = default_loss,
    iterate_batches: callable = iterate_batches,
):
    all_losses = []
    ntokens = 0

    index_iterator = iter(range(num_batches)) if num_batches != -1 else iter(int, 1)

    for _, batch in zip(
        index_iterator,
        iterate_batches(
            dataset=dataset,
            tokenizer=tokenizer,
            batch_size=batch_size,
            max_seq_length=max_seq_length,
        ),
    ):
        losses, toks = loss(model, *batch)
        all_losses.append((losses * toks).item())
        ntokens += toks.item()

    return np.sum(all_losses) / ntokens


class TrainingCallback:

    def on_train_loss_report(self, train_info: dict):
        """Called to report training loss at specified intervals."""
        pass

    def on_val_loss_report(self, val_info: dict):
        """Called to report validation loss at specified intervals or the beginning."""
        pass


def train(
    model,
    tokenizer,
    optimizer,
    train_dataset,
    val_dataset,
    args: TrainingArgs = TrainingArgs(),
    loss: callable = default_loss,
    iterate_batches: callable = iterate_batches,
    training_callback: TrainingCallback = None,
):
    print(f"Starting training..., iters: {args.iters}")

    if args.grad_checkpoint:
        grad_checkpoint(model.layers[0])

    state = [model.state, optimizer.state]

    def step(batch):
        # Forward and backward pass
        (lvalue, toks), grad = loss_value_and_grad(model, *batch)

        # Model update
        optimizer.update(model, grad)

        return lvalue, toks

    loss_value_and_grad = nn.value_and_grad(model, loss)

    losses = []
    n_tokens = 0
    trained_tokens = 0
    # Main training loop
    start = time.perf_counter()
    for it, batch in zip(
        range(1, args.iters + 1),
        iterate_batches(
            dataset=train_dataset,
            tokenizer=tokenizer,
            batch_size=args.batch_size,
            max_seq_length=args.max_seq_length,
            train=True,
        ),
    ):
        # Report validation loss if needed, the first validation loss
        # is always measured before any training.
        if it == 1 or it % args.steps_per_eval == 0 or it == args.iters:
            stop = time.perf_counter()
            val_loss = evaluate(
                model=model,
                dataset=val_dataset,
                loss=loss,
                tokenizer=tokenizer,
                batch_size=args.batch_size,
                num_batches=args.val_batches,
                max_seq_length=args.max_seq_length,
                iterate_batches=iterate_batches,
            )
            val_time = time.perf_counter() - stop
            print(
                f"Iter {it}: " f"Val loss {val_loss:.3f}, " f"Val took {val_time:.3f}s"
            )

            if training_callback is not None:
                val_info = {
                    "iteration": it,
                    "val_loss": val_loss,
                    "val_time": val_time,
                }
                training_callback.on_val_loss_report(val_info)

            start = time.perf_counter()

        lvalue, toks = step(batch)
        mx.eval(state, lvalue, toks)

        # Record loss
        losses.append(lvalue.item())
        n_tokens += toks.item()

        # Report training loss if needed
        if it % args.steps_per_report == 0 or it == args.iters:
            stop = time.perf_counter()

            train_loss = np.mean(losses)
            learning_rate = optimizer.learning_rate.item()
            it_sec = args.steps_per_report / (stop - start)
            tokens_sec = float(n_tokens) / (stop - start)
            trained_tokens += n_tokens
            peak_mem = mx.metal.get_peak_memory() / 2**30
            print(
                f"Iter {it}: Train loss {train_loss:.3f}, "
                f"Learning Rate {learning_rate:.3e}, "
                f"It/sec {it_sec:.3f}, "
                f"Tokens/sec {tokens_sec:.3f}, "
                f"Trained Tokens {trained_tokens}, "
                f"Peak mem {peak_mem:.3f} GB"
            )

            if training_callback is not None:
                train_info = {
                    "iteration": it,
                    "train_loss": train_loss,
                    "learning_rate": learning_rate,
                    "iterations_per_second": it_sec,
                    "tokens_per_second": tokens_sec,
                    "trained_tokens": trained_tokens,
                    "peak_memory": peak_mem,
                }
                training_callback.on_train_loss_report(train_info)

            losses = []
            n_tokens = 0
            start = time.perf_counter()

        # Save adapter weights
        if it % args.steps_per_save == 0:
            save_adapter(model, args.adapter_file)
            checkpoint = (
                Path(args.adapter_file).parent / f"{it:07d}_adapters.safetensors"
            )
            save_adapter(model, checkpoint)
            print(
                f"Iter {it}: Saved adapter weights to "
                f"{args.adapter_file} and {checkpoint}."
            )

    # save final adapter weights
    save_adapter(model, args.adapter_file)
    print(f"Saved final adapter weights to {args.adapter_file}.")


def save_adapter(
    model: nn.Module,
    adapter_file: Union[str, Path],
):
    flattened_tree = tree_flatten(model.trainable_parameters())
    mx.save_safetensors(str(adapter_file), dict(flattened_tree))
