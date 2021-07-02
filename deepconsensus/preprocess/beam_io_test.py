"""Tests for beam_io module."""

import shutil

from absl.testing import absltest
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.testing.util import is_empty

from deepconsensus.preprocess import beam_io
from deepconsensus.utils.test_utils import deepconsensus_testdata
from google3.third_party.nucleus.protos import bed_pb2
from google3.third_party.nucleus.protos import reads_pb2


class TestReadSam(absltest.TestCase):

  def _read_from_bam_is_valid(self, num_expected):

    def _equal(actual):
      self.assertLen(actual, num_expected)
      for read in actual:
        self.assertIsInstance(read, reads_pb2.Read)

    return _equal

  def test_process_single_file(self):
    input_path = deepconsensus_testdata('ecoli/ecoli.truthToCcs.bam')
    with TestPipeline() as p:
      result = (p | beam_io.ReadSam(input_path))
      # All alignments should be kept.
      assert_that(result, self._read_from_bam_is_valid(105))

  def test_process_single_file_no_secondary(self):
    input_path = deepconsensus_testdata('ecoli/ecoli.truthToCcs.bam')
    read_requirements = reads_pb2.ReadRequirements()
    with TestPipeline() as p:
      result = (
          p | beam_io.ReadSam(input_path, read_requirements=read_requirements))
      # All alignments are secondary, so should be filtered out as
      # read_requirements.keep_secondary_alignments is False by default.
      assert_that(result, is_empty())

  def test_process_single_file_keep_secondary(self):
    input_path = deepconsensus_testdata('ecoli/ecoli.truthToCcs.bam')
    read_requirements = reads_pb2.ReadRequirements(
        keep_secondary_alignments=True)
    with TestPipeline() as p:
      result = (
          p | beam_io.ReadSam(input_path, read_requirements=read_requirements))
      # All alignments should be kept as
      # read_requirements.keep_secondary_alignments is set to True.
      assert_that(result, self._read_from_bam_is_valid(105))

  def test_process_multiple_files(self):
    file_pattern = deepconsensus_testdata('ecoli/*.bam')
    read_requirements = reads_pb2.ReadRequirements(
        keep_secondary_alignments=True)
    with TestPipeline() as p:
      result = (
          p
          | beam_io.ReadSam(file_pattern, read_requirements=read_requirements))
      # All reads from `subreads_aligned_to_ccs_one_contig.bam` and
      # `truth_aligned_to_ccs_one_contig.bam` should be kept. The read from
      # `one_ccs.bam` should not be kept as it is unmapped.
      assert_that(result, self._read_from_bam_is_valid(361))


class TestReadIndexedFasta(absltest.TestCase):

  def _read_from_indexed_fasta_is_valid(self):

    def _equal(actual):
      self.assertLen(actual, 1)
      contig, sequence = actual[0]
      self.assertEqual(contig, 'm54316_180808_005743/5636304/truth')
      self.assertSetEqual(set(sequence), set(['A', 'C', 'G', 'T']))

    return _equal

  def test_process_single_file(self):
    input_path = deepconsensus_testdata('ecoli/ecoli.truth.fasta')
    with TestPipeline() as p:
      result = (p | beam_io.ReadIndexedFasta(input_path))
      assert_that(result, self._read_from_indexed_fasta_is_valid())

  def test_process_single_file_with_cache_size(self):
    input_path = deepconsensus_testdata('ecoli/ecoli.truth.fasta')
    with TestPipeline() as p:
      result = (p | beam_io.ReadIndexedFasta(input_path, cache_size=0))
      assert_that(result, self._read_from_indexed_fasta_is_valid())


class TestReadPlainTextFasta(absltest.TestCase):

  def _read_from_plaintext_fasta_valid(self, exp_count):

    def _equal(actual):
      self.assertLen(actual, exp_count)

    return _equal

  def test_read_plain_fasta(self):
    n_reads = 2
    input_path = deepconsensus_testdata('human/human.truth.fasta')
    with TestPipeline() as p:
      result = (p | beam_io.ReadFastaFile(input_path))
      assert_that(result, self._read_from_plaintext_fasta_valid(n_reads))

  def test_read_multiple_fasta(self):
    n_reads = 8
    input_path = deepconsensus_testdata('human/human.truth.fasta')
    fasta_cp = self.create_tempfile().full_path
    shutil.copy(input_path, fasta_cp)
    shutil.copy(fasta_cp, fasta_cp + '1')
    shutil.copy(fasta_cp, fasta_cp + '2')
    shutil.copy(fasta_cp, fasta_cp + '3')
    with TestPipeline() as p:
      result = (p | beam_io.ReadFastaFile(fasta_cp + '*'))
      assert_that(result, self._read_from_plaintext_fasta_valid(n_reads))


class TestReadBed(absltest.TestCase):

  def _read_from_bed_is_valid(self, read_all_fields=True):

    def _equal(actual):
      self.assertLen(actual, 1)
      record = actual[0]
      self.assertEqual(record.reference_name, 'ecoliK12_pbi_August2018')
      self.assertEqual(record.start, 2332251)
      self.assertEqual(record.end, 2347972)

      if read_all_fields:
        self.assertEqual(record.name, 'm54316_180808_005743/5636304/ccs')
        self.assertEqual(record.score, 0.0017145)
        self.assertEqual(record.strand, bed_pb2.BedRecord.REVERSE_STRAND)
      else:
        # Only three columns read, so these fields should have default value.
        self.assertEqual(record.name, '')
        self.assertEqual(record.score, 0)
        self.assertEqual(record.strand, bed_pb2.BedRecord.NO_STRAND)

    return _equal

  def test_process_single_file(self):
    input_path = deepconsensus_testdata('ecoli/ecoli.refCoords.bed')
    with TestPipeline() as p:
      result = (p | beam_io.ReadBed(input_path))
      assert_that(result, self._read_from_bed_is_valid())

  def test_process_single_file_with_num_fields(self):
    input_path = deepconsensus_testdata('ecoli/ecoli.refCoords.bed')
    with TestPipeline() as p:
      result = (p | beam_io.ReadBed(input_path, num_fields=3))
      assert_that(result, self._read_from_bed_is_valid(read_all_fields=False))


if __name__ == '__main__':
  absltest.main()
