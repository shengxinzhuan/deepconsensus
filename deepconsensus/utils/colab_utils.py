# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for error analysis that can be used in colab."""

import os
import random
from typing import Generator, List, Tuple

import ml_collections
import numpy as np
import pandas as pd
import tensorflow as tf

from deepconsensus.models import data_providers
from deepconsensus.models import majority_vote_transforms
from deepconsensus.models import model_utils
from deepconsensus.protos import deepconsensus_pb2
from deepconsensus.tf_examples import tf_example_utils
from deepconsensus.utils import dc_constants

from nucleus.protos import position_pb2
from nucleus.protos import reads_pb2
from nucleus.util import cigar

WRITE_NORMAL = '\x1b[0m'
WRITE_GREEN_BACKGROUND = '\x1b[102m'
WRITE_RED_BACKGROUND = '\x1b[101m'
WRITE_YELLOW_BACKGROUND = '\x1b[103m'

KMER_SIZE = 10


def remove_gaps(seq: str) -> str:
  """Removes gaps and padding from sequences."""
  seq = seq.replace(dc_constants.GAP_OR_PAD, '')
  return seq




def get_deepconsensus_prediction(
    model: tf.keras.Model, rows: tf.Tensor) -> Tuple[tf.Tensor, tf.Tensor]:
  """Runs model on given rows and returns distributions and predictions."""
  softmax_output = model(rows, training=False)
  pred = tf.argmax(softmax_output, axis=-1)
  return softmax_output, pred


def get_mv_pred(dc_input: deepconsensus_pb2.DeepConsensusInput) -> str:
  """Runs majority vote on the input data and return the prediction."""
  do_fn = majority_vote_transforms.GetConsensusFromMajorityVoteDoFn()
  _, mv_pred_bases, _ = next(iter(do_fn.process(dc_input)))
  return mv_pred_bases


def check_has_errors(label: str, pred: str) -> bool:
  """True if there are errors in the prediction, else False."""
  return remove_gaps(label) != remove_gaps(pred)


def ints_to_bases(bases_row: tf.Tensor) -> str:
  """Converts ints to bases based on order in the vocab."""
  return ''.join([dc_constants.VOCAB[int(b)] for b in bases_row])


def convert_to_bases(rows: tf.Tensor, label: tf.Tensor,
                     deepconsensus_pred: tf.Tensor,
                     max_passes: int) -> Tuple[List[str], str, str]:
  """Converts numerical tensors to string of bases."""
  rows = tf.squeeze(rows)
  label = tf.squeeze(label)
  deepconsensus_pred = tf.squeeze(deepconsensus_pred)
  base_indices, _, _, _, _, _ = tf_example_utils.get_indices(max_passes)
  subread_rows_range = range(*base_indices)
  subread_rows = [rows[i, :].numpy() for i in subread_rows_range]
  subread_rows = [row for row in subread_rows if np.sum(row) != 0]
  subread_bases = [ints_to_bases(subread_row) for subread_row in subread_rows]

  label_bases = ints_to_bases(label)
  deepconsensus_pred_bases = ints_to_bases(deepconsensus_pred)
  return subread_bases, label_bases, deepconsensus_pred_bases


def pretty_print_proto(dc_input, print_aux=False):
  """Prints fields from the given DeepConsensusInput proto."""
  spaces = 3 if print_aux else 0
  bases_list = list(str(dc_input.label.bases))
  print('Label:')
  print(''.join([' ' * spaces + base for base in bases_list]))
  print('\n')
  print('Subreads:')
  for read in dc_input.subreads:
    bases_list = list(str(read.bases))
    print(''.join([' ' * spaces + base for base in bases_list]))
  if print_aux:
    print('\n')
    print('PW:')
    for read in dc_input.subreads:
      bases_list = list(str(read.bases))
      print(''.join(['%4d' % value for value in read.pw]))
    print('\n')
    print('IP:')
    for read in dc_input.subreads:
      bases_list = list(str(read.bases))
      print(''.join(['%4d' % value for value in read.ip]))
    print('\n')
    print('Strand:')
    for read in dc_input.subreads:
      print('%4d' % read.subread_strand * len(read.bases))


def dataset_generator_fn(
    params: ml_collections.ConfigDict,
    dataset_path: str,
    filter_fn=None
) -> Generator[Tuple[tf.Tensor, tf.Tensor, tf.Tensor,
                     deepconsensus_pb2.DeepConsensusInput], None, None]:
  """Yields fields from the tf.Examples at the input dataset_path."""
  # Freeze params
  random.seed(params.seed)
  tf.random.set_seed(params.seed)
  params.num_epochs = 1
  params.batch_size = 1
  params.default_batch_size = params.batch_size
  params.buffer_size = 100
  params = ml_collections.FrozenConfigDict(params)
  dataset = data_providers.get_dataset_with_metadata(
      file_pattern=os.path.join(dataset_path, '*'),
      num_epochs=params.num_epochs,
      batch_size=params.batch_size,
      params=params)
  for rows, label, num_passes, encoded_dc_input in dataset:
    dc_input = deepconsensus_pb2.DeepConsensusInput.FromString(
        encoded_dc_input[0].numpy())
    if filter_fn is not None and not filter_fn(dc_input):
      continue
    yield rows, label, num_passes, dc_input


def run_models_and_view_predictions(
    params: ml_collections.ConfigDict,
    checkpoint_path: str,
    dataset_path: str,
    dc_errors_only: bool = True,
    output_diff_ccs: bool = True,
    filter_fn=None
) -> Generator[Tuple[List[str], str, str, str, str,
                     deepconsensus_pb2.DeepConsensusInput], None, None]:
  """Runs the DeepConsensus model and majority vote and prints predictions."""
  # Freeze params
  random.seed(params.seed)
  tf.random.set_seed(params.seed)
  params.num_epochs = 1
  params.batch_size = 1
  params.default_batch_size = params.batch_size
  params.buffer_size = 100
  params = ml_collections.FrozenConfigDict(params)
  dataset = data_providers.get_dataset_with_metadata(
      file_pattern=os.path.join(dataset_path, '*'),
      num_epochs=params.num_epochs,
      batch_size=params.batch_size,
      params=params)

  model = model_utils.get_model(params)
  try:
    model.load_weights(checkpoint_path)
  except AssertionError:
    # Use this approach for models saved in tf.train.Checkpoint format through
    # the custom training loop code.
    checkpoint = tf.train.Checkpoint(model=model)
    checkpoint.restore(checkpoint_path)

  for rows, label, _, encoded_dc_input in dataset:
    dc_input = deepconsensus_pb2.DeepConsensusInput.FromString(
        encoded_dc_input[0].numpy())
    if filter_fn is not None and not filter_fn(dc_input):
      continue
    _, deepconsensus_pred = get_deepconsensus_prediction(model, rows)
    mv_pred_bases = get_mv_pred(dc_input)
    subread_bases, label_bases, deepconsensus_pred_bases = convert_to_bases(
        rows, label, deepconsensus_pred, params.max_passes)
    has_errors = check_has_errors(label_bases, deepconsensus_pred_bases)
    if dc_errors_only and not has_errors:
      continue
    # Skip examples where DC makes no changes to the CCS prediction.
    if output_diff_ccs and remove_gaps(
        dc_input.ccs_sequence) == remove_gaps(deepconsensus_pred_bases):
      continue
    yield (subread_bases, label_bases, deepconsensus_pred_bases, mv_pred_bases,
           dc_input.ccs_sequence, dc_input)


def get_results_df(experiments: List[int],
                   experiment_pattern: str,
                   decimals: int = 5) -> pd.DataFrame:
  """Returns a dataframe with inference results."""
  all_lines = None
  for experiment in experiments:
    # `experiment_pattern` should contain '{}' that can be filled in with the
    # experiment number.
    inference_csvs = tf.io.gfile.glob(experiment_pattern.format(experiment))
    for inference_csv in inference_csvs:
      n_rows = 2
      curr_df = pd.read_csv(tf.io.gfile.GFile(inference_csv), nrows=n_rows)
      curr_df['experiment_and_work_unit'] = [
          '/'.join(inference_csv.split('/')[-3:-1])
      ] * n_rows
      curr_df['dataset_type'] = ['eval', 'hard_eval']
      if all_lines is None:
        all_lines = curr_df
      else:
        all_lines = pd.concat([all_lines, curr_df], ignore_index=True)
  assert all_lines is not None
  cols = all_lines.columns.tolist()
  reordered_columns = cols[-2:] + cols[1:-2] + [cols[0]]
  all_lines = all_lines[reordered_columns]
  return all_lines.round(decimals)


def get_results_df_compact(df: pd.DataFrame) -> pd.DataFrame:
  """Returns a compact version of the results with fewer columns."""
  cols_to_keep = [
      'dataset_type', 'experiment_and_work_unit', 'accuracy',
      'per_example_accuracy'
  ]
  return df[cols_to_keep]