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
"""Tests for deepconsensus.models.model_inference_transforms."""

import glob
import os

from absl.testing import absltest
from absl.testing import parameterized
import apache_beam as beam
from apache_beam.testing import test_pipeline
import apache_beam.testing.util as beam_testing_util
import numpy as np
import pandas as pd

from deepconsensus.models import model_configs
from deepconsensus.models import model_inference_transforms
from deepconsensus.models import model_utils
from deepconsensus.tf_examples import tf_example_utils
from deepconsensus.utils import dc_constants
from deepconsensus.utils import test_utils


class ParseTfExamplesDoFnTest(absltest.TestCase):

  def _parsed_example_correct(self, expected_output):

    def _check_dc_input_proto(outputs):
      rows, label, num_passes, dc_input = outputs[0]
      self.assertEqual(rows.tostring(), expected_output[0])
      self.assertEqual(label.tostring(), expected_output[1])
      self.assertEqual(num_passes, expected_output[2])
      self.assertEqual(dc_input, expected_output[3])

    return _check_dc_input_proto

  def test_parse_tf_examples(self):
    """Checks that tf.Examples are correctly read in and parsed."""
    params = model_configs.get_config('fc+test')
    model_utils.modify_params(params)
    # Create DeepConsensusInput and convert to tf.Example.
    example_height = tf_example_utils.get_total_rows(
        max_passes=params.max_passes)
    expected_dc_input = test_utils.make_deepconsensus_input()
    tf_example = tf_example_utils.deepconsensus_input_to_example(
        deepconsensus_input=expected_dc_input, example_height=example_height)
    params.max_length = len(expected_dc_input.label.bases)
    expected_rows = tf_example_utils.get_encoded_subreads_from_example(
        tf_example)
    expected_label = tf_example_utils.get_encoded_label_from_example(tf_example)
    expected_num_passes = tf_example_utils.get_num_passes_from_example(
        tf_example)
    expected_output = (expected_rows, expected_label, expected_num_passes,
                       expected_dc_input)

    # We will serialize the tf.Example and see if ParseTfExamplesDoFn is able to
    # recover the original DeepConsensusInput and tf.Example fields.
    with test_pipeline.TestPipeline() as p:
      output = (
          p
          | beam.Create([tf_example.SerializeToString()])
          | beam.ParDo(
              model_inference_transforms.ParseTfExamplesDoFn(params=params)))
      beam_testing_util.assert_that(
          output, self._parsed_example_correct(expected_output))


class RunForwardPassDoFnTest(absltest.TestCase):

  def _has_valid_prediction(self, params):

    def _check_prediction(outputs):
      dc_input = outputs[0]
      self.assertLen(dc_input.deepconsensus_prediction, params.max_length)
      self.assertContainsSubset(
          set(dc_input.deepconsensus_prediction), dc_constants.VOCAB)
      self.assertNotEmpty(dc_input.quality_string)

    return _check_prediction

  def test_run_forward_pass(self):
    """Checks that forward pass saves valid prediction in DeepConsensusInput."""
    checkpoint_path = test_utils.deepconsensus_testdata('model/checkpoint-1')
    config_name = 'transformer_learn_values+test'
    params = model_configs.get_config(config_name)
    model_utils.modify_params(params)

    # Use dummy data for this test.
    rows = np.random.rand(params.hidden_size, params.max_length,
                          params.num_channels)
    label = np.random.randint(len(dc_constants.VOCAB), size=(params.max_length))
    num_passes = 10
    dc_input = test_utils.make_deepconsensus_input()
    with test_pipeline.TestPipeline() as p:
      records = (
          p
          | beam.Create([(rows, label, num_passes, dc_input)])
          | beam.ParDo(
              model_inference_transforms.RunForwardPassDoFn(
                  checkpoint_path=checkpoint_path, params=params)))
      dc_preds = records | beam.Map(lambda record: record['dc_proto'])
      beam_testing_util.assert_that(dc_preds,
                                    self._has_valid_prediction(params))


class EditDistanceTest(parameterized.TestCase):

  @parameterized.parameters([
      ['ATCG', 'ATCG', 0],
      ['ATCG', 'TT', 3],
      ['ATCG', 'ZZZZ', 4],
      [' A T C G  ', 'ATCG', 0],
  ])
  def test_edit_distance(self, str1, str2, expected_edit_distance):
    ed = model_inference_transforms.edit_distance(str1, str2)
    self.assertEqual(ed, expected_edit_distance)


class RepeatContentTest(parameterized.TestCase):

  @parameterized.parameters([['      ', 0.0], ['ABCD', 0.0], ['AAABBBCD', 0.75],
                             ['AAABBBCCCDDD', 1.0],
                             ['AAA BBB CCC DDD    ', 1.0]])
  def test_repeat_content(self, seq, expected_homopolymer_content):
    hcontent = model_inference_transforms.homopolymer_content(seq)
    self.assertEqual(hcontent, expected_homopolymer_content)


class StatsTest(absltest.TestCase):
  """This will test both Stats and StatsToCsv."""

  def setUp(self):
    super(StatsTest, self).setUp()
    self.metric_name = 'metric'
    self.group_name = 'group'
    self.input_data = pd.DataFrame({
        'metric': np.random.randint(0, 100, 100).astype(float),
        'group': np.random.randint(0, 20, 100),
    }).to_dict('records')

  def _check_stats(self):

    df = pd.DataFrame(self.input_data)

    def _check(stat_output):
      # Test the first result
      group_val, data = stat_output[0]
      df_grouped = df.where(df['group'] == group_val).groupby(
          'group', as_index=True)
      np_mean = float(df_grouped.mean().values)
      np_stdev = float(df_grouped.std(ddof=0).values)
      np_min = float(df_grouped.min().values)
      np_max = float(df_grouped.max().values)
      np_count = float(df_grouped.count().values)
      self.assertAlmostEqual(np_mean, data['mean'])
      self.assertAlmostEqual(np_stdev, data['stddev'])
      self.assertEqual(np_min, data['min'])
      self.assertEqual(np_max, data['max'])
      self.assertEqual(np_count, data['count'])
      self.assertEqual(self.metric_name, data['metric'])
      self.assertEqual(self.group_name, data['group'])

    return _check

  def test_stats_fn(self):

    with test_pipeline.TestPipeline() as p:

      data = (p | beam.Create(self.input_data))
      stats_out = (
          data
          | beam.Map(lambda x: (x['group'], x['metric']))
          | beam.CombinePerKey(
              model_inference_transforms.Stats(self.group_name,
                                               self.metric_name)))

      beam_testing_util.assert_that(stats_out, self._check_stats())

  def test_stats_csv(self):
    out_dir = self.create_tempdir().full_path
    with test_pipeline.TestPipeline() as p:
      data = (p | beam.Create(self.input_data))
      _ = (
          data
          | model_inference_transforms.StatsToCsv(out_dir, self.group_name,
                                                  self.metric_name))

    # Read the output CSV, parse, and convert back to dict format and check.
    fname = f'{self.metric_name}__{self.group_name}'
    output_path = os.path.join(out_dir, 'metrics', self.metric_name, fname)
    df = pd.read_csv(output_path + '.stat.csv')
    df = [(x['group_val'], x) for x in df.to_dict('records')]
    self._check_stats()(df)




if __name__ == '__main__':
  absltest.main()