# ALBERT
For more information about training deep learning models on Gaudi, visit [developer.habana.ai](https://developer.habana.ai/resources/).

Please visit [this page](https://developer.habana.ai/resources/habana-training-models/#performance) for performance information.

## Table of Contents

* [Model Overview](#model-overview)
* [Setup](#setup)
* [ALBERT Fine-Tuning](#albert-fine-tuning)
* [Downloading the datasets](#downloading-the-datasets)
* [Training the Model](#training-the-model)
* [Examples](#examples)
* [Advanced](#advanced)
* [Changelog](#changelog)

## Model Overview

ALBERT is "A Lite" version of BERT, a popular unsupervised language representation learning algorithm by Google. ALBERT uses parameter-reduction techniques that allow for large-scale configurations, overcome previous memory limitations, and achieve better behavior with respect to model degradation.

This release supports Albert Finetuning on 1 and 8 cards.

Our implementation is a fork of [Google Research ALBERT](https://github.com/google-research/albert). Please visit [this page](../../../README.md#tensorflow-model-performance) for performance information.

## Setup
Please follow the instructions given in the following link for setting up the
environment including the `$PYTHON` environment variable: [Gaudi Setup and
Installation Guide](https://github.com/HabanaAI/Setup_and_Install). Please
answer the questions in the guide according to your preferences. This guide will
walk you through the process of setting up your system to run the model on
Gaudi.

## ALBERT Fine-Tuning
- Suited for tasks:
    - `mrpc`: Microsoft Research Paraphrase Corpus (**MRPC**) is a paraphrase identification dataset, where systems aim to identify if two sentences are paraphrases of each other.
    - `squad`: Stanford Question Answering Dataset (**SQuAD**) is a reading comprehension dataset, consisting of
       questions posed by crowdworkers on a set of Wikipedia articles, where the answer to every question is a segment
       of text, or span, from the corresponding reading passage, or the question might be unanswerable.
- Default hyperparameters:
    - dataset: squad
    - predict_batch_size: 8
    - max_seq_length: 384
    - doc_stride: 128
    - max_query_length: 64
    - learning_rate: 5e-5
    - num_train_epochs: 2.0
    - warmup_proportion: 0.1
    - save_checkpoints_steps: 5000
    - do_lower_case: true
    - do_train: true
    - do_predict: true
    - use_einsum: false
    - n_best_size: 20
    - max_answer_length: 30
- The output will be saved in $HOME/tmp by default.

## Downloading the datasets
For finetuning task, since it is using the same datasets as in bert, please follow the steps in Model-References/TensorFlow/nlp/bert/README.md section "Download and preprocess the datasets for Pretraining and Finetuning"

## Training the Model

In the docker container, clone this repository and switch to the branch that
matches your SynapseAI version. (Run the
[`hl-smi`](https://docs.habana.ai/en/latest/System_Management_Tools_Guide/System_Management_Tools.html#hl-smi-utility-options)
utility to determine the SynapseAI version.)

```bash
git clone -b [SynapseAI version] https://github.com/HabanaAI/Model-References
```
Go to the ALBERT directory:

```bash
cd Model-References/TensorFlow/nlp/albert
pip install -r requirements.txt
```

If Model-References repository path is not in the PYTHONPATH, make sure you update it:
```bash
export PYTHONPATH=$PYTHONPATH:/path/to/Model-References
```

## Examples
The training can be run with a custom python script `albert_demo.py` usage:

```python
$PYTHON demo_albert.py --command <command> --model_variant <model> --data_type <data_type> --test_set <dataset_name> --dataset_path <path/to/dataset> --output_dir <model/data/path>
```

The following examples assume that the datasets are in a directory /data/tensorflow/ :

-  Single Gaudi card finetuning of albert Large, using MRPC dataset on bfloat16 precision:
```python
$PYTHON demo_albert.py --command finetuning --model_variant large --data_type bf16 --batch_size 32 --test_set mrpc --output_dir /root/tmp/albert_large --dataset_path /data/tensorflow/bert/MRPC/
```
-  Single Gaudi card finetuning of albert Large, using SQuAD dataset on bfloat16 precision:
```python
$PYTHON demo_albert.py --command finetuning --model_variant large --data_type bf16 --batch_size 32 --test_set squad --output_dir /root/tmp/albert_large --dataset_path /data/tensorflow/bert/SQuAD/
```
- 8 Gaudi cards finetuning of ALBERT Large in bfloat16 precision using SQuAD dataset on a single box (8 cards):
  ```bash
  cd /path/to/Model-References/TensorFlow/nlp/albert/

  $PYTHON demo_albert.py \
     --command finetuning \
     --model_variant large \
     --data_type bf16 \
     --batch_size 32 \
     --test_set squad \
     --output_dir /root/tmp/albert_large \
     --dataset_path /data/tensorflow/albert/tf_record/squad \
     --use_horovod 8 \
  2>&1 | tee ~/hlogs/albert_large_finetuning_bf16_squad_8_cards.txt
```
The script automatically downloads the pre-trained model from https://storage.googleapis.com/albert_models/ the first time it is run in the docker container, as well as the dataset,if needed.
- 8 Gaudi cards finetuning of ALBERT Large in bfloat16 precision using SQuAD dataset on a K8s single box (8 cards):
*<br>mpirun map-by PE attribute value may vary on your setup and should be calculated as:<br>
socket:PE = floor((number of physical cores) / (number of gaudi devices per each node))*
```bash
  mpirun --allow-run-as-root \
         --bind-to core \
         --map-by socket:PE=6 \
         -np 8 \
         --tag-output \
         --merge-stderr-to-stdout \
         bash -c "cd /root/Model-References/TensorFlow/nlp/albert;\
                  $PYTHON /root/Model-References/TensorFlow/nlp/albert/demo_albert.py \
                   --model_variant=large \
                   --command=finetuning \
                   --test_set=squad \
                   --data_type=bf16 \
                   --epochs=2 \
                   --batch_size=32 \
                   --max_seq_length=384 \
                   --learning_rate=3e-5 \
                   --output_dir=$HOME/tmp/squad_output_8cards/ \
                   --dataset_path=/data/tensorflow/albert/tf_record/squad \
                   --use_horovod=1 \
                   --kubernetes_run=True" \
  2>&1 | tee ~/hlogs/albert_large_ft_squad_8cards.txt
```

## Advanced
### Scripts
* `demo_albert.py`: Demo distributed luncher script, enables single and mutlinode training for finetuning task.
* `run_classifier.py`:  Script implementing MRPC task.
* `run_squad_v1.py`:  Script implementing SQUAD task.

## Changelog
### 1.2.0
* cleanup script from deprecated habana_model_runner
### 1.3.0
* adding handling of save_checkpoints_steps parameter and change default to 5000
* removal obsolete demo_albert (bash script)
* move BF16 config json file from TensorFlow/common/ to model's dir
* update requirements.txt
* remove redundant imports