"""TF2 + tf.keras implementations of networks for DeepConsensus."""

import logging
from typing import Callable, Optional, Tuple

import ml_collections
import tensorflow as tf

from deepconsensus.tf_examples import tf_example_utils
from tensorflow_models.official.nlp import modeling
from tensorflow_models.official.nlp.bert import bert_models
from tensorflow_models.official.nlp.bert import configs
from tensorflow_models.official.nlp.transformer import embedding_layer
from tensorflow_models.official.nlp.transformer import model_utils
from tensorflow_models.official.nlp.transformer import transformer


# pylint: disable=invalid-name
def FullyConnectedNet(params: ml_collections.ConfigDict) -> tf.keras.Model:
  """Fully connected neural network architecture."""

  inputs = tf.keras.Input(
      shape=(params.hidden_size, params.max_length, params.num_channels))
  l2_reg = tf.keras.regularizers.l2
  net = inputs
  net = tf.keras.layers.Flatten()(net)
  for i in range(len(params.fc_size)):
    net = tf.keras.layers.Dense(
        units=params.fc_size[i],
        activation=tf.nn.relu,
        kernel_regularizer=l2_reg(params.l2))(
            net)
    net = tf.keras.layers.Dropout(rate=params.fc_dropout)(net)

  net = tf.keras.layers.Dense(units=params.max_length * params.num_classes)(net)
  net = tf.keras.layers.Reshape((params.max_length, params.num_classes))(net)
  net = tf.keras.layers.Softmax(axis=-1)(net)
  outputs = net
  return tf.keras.Model(inputs=inputs, outputs=outputs)


def get_conv_sub_model(
    conv_model
) -> Tuple[Callable[..., tf.Tensor], Callable[[tf.keras.Model],
                                              tf.keras.Model]]:
  """Returns a predefined convolutional architecture."""
  if conv_model == 'resnet50':
    return tf.keras.applications.ResNet50V2, tf.keras.applications.resnet_v2.preprocess_input
  elif conv_model == 'resnet101':
    return tf.keras.applications.ResNet101V2, tf.keras.applications.resnet_v2.preprocess_input
  elif conv_model == 'resnet152':
    return tf.keras.applications.ResNet152V2, tf.keras.applications.resnet_v2.preprocess_input
  else:
    raise NotImplementedError(f'conv model "{conv_model}" not found')


# pylint: disable=invalid-name
class ConvNet(tf.keras.Model):
  """Convolutional neural network architecture."""

  def __init__(self, params: ml_collections.ConfigDict, **kwargs):
    super(ConvNet, self).__init__(params, **kwargs)
    # Most conv models only accept 3 channels.
    self.resnet_input_shape = (params.hidden_size, params.max_length, 3)
    self.dimensions = params.max_length * params.num_classes

    model, self.conv_preprocess = get_conv_sub_model(params.conv_model)
    self.model = model(
        include_top=False,
        weights=None,
        input_shape=self.resnet_input_shape,
        pooling='avg')
    self.use_sn = params.use_sn
    self.max_length = params.max_length
    self.num_classes = params.num_classes

    # Define layers
    self.layer_dense = tf.keras.layers.Dense(units=self.dimensions)

  def call(self, inputs: tf.Tensor, training: bool) -> tf.Tensor:
    # Most conv models only accept 3 channels;
    # The sn channel must be removed and optionally
    # added back at the end. CCS rows not being used currently for this model.
    input_rows, _, sn_rows = tf.split(inputs, [3, 1, 1], 3)

    cn_input = self.conv_preprocess(input_rows)
    net = self.model(cn_input, training=training)

    if self.use_sn:
      logging.info('Using SN Values')
      # sn_rows was padded previously to match the input dimensions
      # Crop it here back to 4 rows.
      sn_rows = tf.image.crop_to_bounding_box(sn_rows, 0, 0, 4, self.max_length)
      sn_rows = tf.keras.layers.Flatten()(sn_rows)
      net = tf.keras.layers.Flatten()(net)
      net = tf.concat([net, sn_rows], 1)
    else:
      net = tf.keras.layers.Flatten()(net)

    net = self.layer_dense(net)
    net = tf.keras.layers.Reshape((self.max_length, self.num_classes))(net)
    net = tf.keras.layers.Softmax(axis=-1)(net)
    output = net
    return output


class EncoderOnlyTransformer(transformer.Transformer):
  """Modified encoder-only transformer model for DeepConsensus.

  This implementation extends the one in
  https://github.com/tensorflow/models/blob/master/official/nlp/transformer/transformer.py. The
  main changes are:

  * Removing logic relating to converting tokens to embeddings, since the
  DeepConsensus is already in the form of vectors for each position.

  * Removing the decoder, since we only want to run the encoder.

  * Adding additional layers on top of the encoder for the per-position
  classification task.
  """

  def __init__(self,
               params: ml_collections.ConfigDict,
               name: Optional[str] = None):
    # Call grandparent super since we don't want to initialize embeddings.
    super(transformer.Transformer, self).__init__(params, name=name)
    self.params = params
    if self.params.add_pos_encoding and self.params.use_relative_pos_enc:
      self.position_embedding = modeling.layers.position_embedding.RelativePositionEmbedding(
          hidden_size=self.params['hidden_size'])
    self.encoder_stack = transformer.EncoderStack(params)
    self.fc1 = tf.keras.layers.Dense(
        units=(params['vocab_size']),
        activation=None,
        use_bias=True,
        kernel_initializer='glorot_uniform',
        bias_initializer='zeros')
    self.softmax = tf.keras.layers.Softmax()

  def call(self, inputs: tf.Tensor, training: bool) -> tf.Tensor:
    """Runs a forward pass of the model.

    Args:
      inputs: tensor of shape (batch_size, hidden_size, input_length
        num_channels).
      training: boolean, whether in training mode or not.

    Returns:
      Output from softmax layer, which is a distribution over the vocabular at
      each position in the sequence.
    """

    with tf.name_scope('Transformer'):

      # Get rid of the channel dimension as we only have one channel.
      inputs = tf.squeeze(inputs, -1)

      # `inputs` is of shape (batch_size, hidden_size, input_length). For the
      # Transformer, we need to change the format to be the following:
      # (batch_size, input_length, hidden_size).
      inputs = tf.transpose(inputs, [0, 2, 1])

      # Attention_bias for our model should be all 0s with shape
      # (batch_size, 1, 1, input_length). See model_utils.get_padding_bias
      # to see how this is calculated in the base model.
      all_zeros = tf.reduce_sum(tf.zeros_like(inputs), -1)
      attention_bias = tf.expand_dims(tf.expand_dims(all_zeros, 1), 1)

      # Run the inputs through the encoder. Encoder returns the softmax output.
      encoder_outputs = self.encode(inputs, attention_bias, training)
      logits = encoder_outputs
      return logits

  def encode(self, inputs: tf.Tensor, attention_bias: tf.Tensor,
             training: bool) -> tf.Tensor:
    """Runs the input through Encoder stack and problem-specific layers."""

    with tf.name_scope('encode'):

      # The input for each position is already a vector, so we do not use
      # embeddings here, unlike the base model. Base model input is a token at
      # each position, which must first be embedded as a vector. In the future,
      # we may want to use embeddings for part of the input, such as the bases,
      # so that we can learn the scale of values.
      encoder_inputs = inputs

      # Positional embedding only works when we have an even value for the
      # hidden_size. If hidden_size is odd, add an empty row to make it even.
      if self.params.add_pos_encoding and encoder_inputs.shape[2] % 2 != 0:
        empty_row = tf.zeros(
            shape=(self.params.batch_size, self.params.max_length, 1))
        encoder_inputs = tf.concat([encoder_inputs, empty_row], axis=-1)
        assert self.params.hidden_size == encoder_inputs.shape[2]

      # All values in `input_padding` should be 0 and shape should be
      # (batch_size, input_length). See model_utils.get_padding to see how this
      # is computed for the base model.
      inputs_padding = tf.reduce_sum(tf.zeros_like(encoder_inputs), -1)

      # Cast input `attention_bias` to correct type, as done in the base model.
      attention_bias = tf.cast(attention_bias, self.params['dtype'])

      # Add positional encoding to the input. The scale of the positional
      # encoding relative to the input values will matter since we are not
      # learning the input embedding.
      if self.params['add_pos_encoding']:
        with tf.name_scope('add_pos_encoding'):
          if self.params['use_relative_pos_enc']:
            pos_encoding = self.position_embedding(inputs=encoder_inputs)
          else:
            pos_encoding = model_utils.get_position_encoding(
                self.params['max_length'], self.params['hidden_size'])
          pos_encoding = tf.cast(pos_encoding, self.params['dtype'])
          encoder_inputs += pos_encoding

      # Add dropout when training.
      if training:
        encoder_inputs = tf.nn.dropout(
            encoder_inputs, rate=self.params['layer_postprocess_dropout'])

      # Pass inputs through the encoder. As mentioned above, `inputs_padding` is
      # not actually used by EncoderStack.call. Encoder stack output has shape
      # (batch_size, input_length, hidden_size).
      encoder_outputs = self.encoder_stack(
          encoder_inputs, attention_bias, inputs_padding, training=training)

      # Pass through dense layer, and output a distribution.
      encoder_outputs = self.fc1(encoder_outputs)
      encoder_outputs = self.softmax(encoder_outputs)
      return encoder_outputs

  def decode(self, encoder_outputs: tf.Tensor, attention_bias: tf.Tensor,
             training: bool) -> tf.Tensor:
    """Returns the outputs from the encoder."""

    raise NotImplementedError

  def predict(self, encoder_inputs: tf.Tensor) -> tf.Tensor:
    """Returns the argmax of the decoder output, which comes from a softmax."""

    # The base model also has a predict method that behaves differently. This
    # predict function is consistent with how predict behaves for other
    # DeepConsensus models (conv, FC), but we may want to change this in the
    # future to match the transformer base class. For more details, see:
    # cs/google3/third_party/tensorflow_models/official/nlp/transformer/transformer.py;l=279
    return self.call(encoder_inputs, training=False)


class EncoderOnlyLearnedValuesTransformer(EncoderOnlyTransformer):
  """Modified transformer that learns embeddings for the bases."""

  def __init__(self,
               params: ml_collections.ConfigDict,
               name: Optional[str] = None):
    super(EncoderOnlyLearnedValuesTransformer, self).__init__(params, name=name)
    if params.use_bases:
      self.bases_embedding_layer = embedding_layer.EmbeddingSharedWeights(
          params['vocab_size'], params['per_base_hidden_size'])
    if params.use_pw:
      pw_vocab_size = params.PW_MAX + 1
      self.pw_embedding_layer = embedding_layer.EmbeddingSharedWeights(
          pw_vocab_size, params['pw_hidden_size'])
    if params.use_ip:
      ip_vocab_size = params.IP_MAX + 1
      self.ip_embedding_layer = embedding_layer.EmbeddingSharedWeights(
          ip_vocab_size, params['ip_hidden_size'])

    # Set params related to DNABERT here for convenience while actively
    # integrating it.
    # <internal>
    # training. At inference time, model.load_weights should do this work.
    if params.use_dnabert:
      logging.info('Loading DNA-BERT model...')
      bert_config = configs.BertConfig.from_json_file(params.bert_config_file)
      # Instantiate the transformer layers of DNA-BERT.
      self.dnabert_encoder = bert_models.get_transformer_encoder(
          bert_config, params.dnabert_max_seq_length)
      # Use a pretrained checkpoint to update the weights.
      checkpoint = tf.train.Checkpoint(model=self.dnabert_encoder)
      checkpoint.restore(params.pretrained_dnabert_checkpoint).run_restore_ops()
      logging.info('Done loading DNA-BERT model.')

      # Define a dense layer to linearly map the dnabert embeddings to a
      # smaller dimension (dnabert_desired_hidden_size) in order to keep the
      # per-base embedding size small.
      self.dnabert_condenser = tf.keras.layers.Dense(
          units=(params.dnabert_desired_hidden_size),
          activation=None,
          use_bias=False,
          kernel_initializer='glorot_uniform',
          bias_initializer='zeros')

      # Define a dense layer to linearly map the concatenated embeddings of
      # all subreads at a given position to a smaller dimension
      # (transformer_input_size) in order to keep the transformer layers small
      # as we feed it more features.
      if self.params.condense_transformer_input:
        self.transformer_input_condenser = tf.keras.layers.Dense(
            units=(params.transformer_input_size),
            activation=None,
            use_bias=False,
            kernel_initializer='glorot_uniform',
            bias_initializer='zeros')

    if params.use_sn:
      sn_vocab_size = params.SN_MAX + 1
      self.sn_embedding_layer = embedding_layer.EmbeddingSharedWeights(
          sn_vocab_size, params['sn_hidden_size'])

    if params.use_strand:
      strand_vocab_size = params.STRAND_MAX + 1
      self.strand_embedding_layer = embedding_layer.EmbeddingSharedWeights(
          strand_vocab_size, params['strand_hidden_size'])

  def encode(self, inputs: tf.Tensor, attention_bias: tf.Tensor,
             training: bool) -> tf.Tensor:
    """Runs the input through Encoder stack and problem-specific layers."""

    # Input to embedding layer is [batch_size, length] and output will be
    # [batch_size, length, embedding_size]. Embed each row of the input
    # separately and then concatenate.
    embedded_inputs = []
    base_indices, pw_indices, ip_indices, strand_indices, ccs_indices, sn_indices = tf_example_utils.get_indices(
        self.params['max_passes'])
    if self.params.use_bases:
      for i in range(*base_indices):
        # Shape: [batch_size, length, per_base_hidden_size]
        embedded = self.bases_embedding_layer(
            tf.cast(inputs[:, :, i], tf.int32))
        embedded_inputs.append(embedded)

    if self.params.use_dnabert:
      for i in range(*base_indices):
        # <internal>
        # Shape: [batch_size, length, 1]
        dnabert_input_sequence = inputs[:, :, i]
        # Shape: [batch_size, length, dnabert_original_hidden_size]
        # https://source.corp.google.com/piper///depot/google3/third_party/tensorflow_models/official/nlp/bert/serving.py;l=55;rcl=326286926

        input_mask = tf.ones_like(dnabert_input_sequence, dtype=tf.int32)
        input_type_ids = tf.zeros_like(dnabert_input_sequence, dtype=tf.int32)

        dnabert_embeddings, _ = self.dnabert_encoder([
            tf.cast(dnabert_input_sequence, tf.int32), input_mask,
            input_type_ids
        ])
        # Shape: [batch_size, length, dnabert_desired_hidden_size]
        condensed_dnabert_embeddings = self.dnabert_condenser(
            dnabert_embeddings)
        embedded_inputs.append(condensed_dnabert_embeddings)

    if self.params.use_pw:
      for i in range(*pw_indices):
        # Shape: [batch_size, length, pw_hidden_size]
        embedded = self.pw_embedding_layer(tf.cast(inputs[:, :, i], tf.int32))
        embedded_inputs.append(embedded)

    if self.params.use_ip:
      for i in range(*ip_indices):
        # Shape: [batch_size, length, ip_hidden_size]
        embedded = self.ip_embedding_layer(tf.cast(inputs[:, :, i], tf.int32))
        embedded_inputs.append(embedded)

    if self.params.use_strand:
      for i in range(*strand_indices):
        embedded = self.strand_embedding_layer(
            tf.cast(inputs[:, :, i], tf.int32))
        embedded_inputs.append(embedded)

    if self.params.use_ccs:
      for i in range(*ccs_indices):
        embedded = self.bases_embedding_layer(
            tf.cast(inputs[:, :, i], tf.int32))
        embedded_inputs.append(embedded)

    # <internal>
    # weights to aggregate subread-level embeddings (instead of concatenating).
    if self.params.use_sn:
      # The last four elements in the last dimension in the inputs tensor
      # correspond to the four signal-to-noise ratio scores for A, G, C, T.
      for i in range(*sn_indices):
        embedded = self.sn_embedding_layer(tf.cast(inputs[:, :, i], tf.int32))
        embedded_inputs.append(embedded)

    # Shape: [batch_size, length, max_passes *
    #          (per_base_hidden_size + dnabert_desired_hidden_size +
    #           pw_hidden_size + ip_hidden_size)]
    embedded_inputs = tf.concat(embedded_inputs, axis=-1)
    embedded_inputs = tf.cast(embedded_inputs, self.params['dtype'])

    if self.params.condense_transformer_input:
      # Condense the transformer input at each position to a smaller vector to
      # reduce the transformer hidden size, since the transformer model size is
      # quadratic in its hidden size.
      # Shape: [batch_size, length, transformer_input_size]
      transformer_input = self.transformer_input_condenser(embedded_inputs)
    else:
      transformer_input = embedded_inputs

    return super(EncoderOnlyLearnedValuesTransformer,
                 self).encode(transformer_input, attention_bias, training)
