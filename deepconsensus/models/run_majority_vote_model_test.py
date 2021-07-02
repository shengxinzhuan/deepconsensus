"""Tests for deepconsensus .run_majority_vote_model."""

import glob
import itertools
import os

from absl.testing import absltest
from absl.testing import parameterized
import apache_beam as beam

from deepconsensus.models import run_majority_vote_model
from deepconsensus.utils import test_utils
from google3.pipeline.flume.py import runner as flume_runner


class RunMajorityVoteModelTest(parameterized.TestCase):

  @parameterized.parameters(
      itertools.product([None, 100], [True, False], ['ecoli', 'human'],
                        ['DeepConsensusInput', 'Example']))
  def test_end_to_end(self, example_width, write_errors, species, proto_class):
    """Test that the full pipeline runs without errors."""

    # Get path to the directory.
    if proto_class == 'DeepConsensusInput':
      input_tfrecords_path = test_utils.deepconsensus_testdata(
          f'{species}/output/deepconsensus/deepconsensus@*.tfrecords.gz')
    elif proto_class == 'Example':
      input_tfrecords_path = test_utils.deepconsensus_testdata(
          f'{species}/output/tf_examples/train/train@*.tfrecords.gz')
    else:
      raise ValueError('Unexpected proto_class')

    # Output path only gets used when write_errors is True.
    output_path = self.create_tempdir().full_path

    # Run the pipeline.
    runner = flume_runner.FlumeRunner()
    pipeline = run_majority_vote_model.create_pipeline(
        input_tfrecords_path=input_tfrecords_path,
        example_width=example_width,
        write_errors=write_errors,
        output_path=output_path,
        proto_class=proto_class)
    options = beam.options.pipeline_options.PipelineOptions(
        pipeline_type_check=True, runtime_type_check=True)
    runner.run(pipeline, options)

    # There should be some output files if write_errors is True.
    if write_errors:
      if proto_class == 'DeepConsensusInput':
        output_files = glob.glob(
            os.path.join(output_path, 'deepconsensus/deepconsensus*'))
      elif proto_class == 'Example':
        output_files = glob.glob(
            os.path.join(output_path, 'deepconsensus/tf_examples*'))
      self.assertNotEmpty(output_files)


if __name__ == '__main__':
  absltest.main()
