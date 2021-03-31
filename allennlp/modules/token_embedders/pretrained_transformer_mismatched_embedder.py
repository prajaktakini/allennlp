from typing import Optional, Dict, Any

from overrides import overrides
import torch

from allennlp.modules.token_embedders import PretrainedTransformerEmbedder, TokenEmbedder
from allennlp.nn import util


@TokenEmbedder.register("pretrained_transformer_mismatched")
class PretrainedTransformerMismatchedEmbedder(TokenEmbedder):
    """
    Use this embedder to embed wordpieces given by `PretrainedTransformerMismatchedIndexer`
    and to get word-level representations.

    Registered as a `TokenEmbedder` with name "pretrained_transformer_mismatched".

    # Parameters

    model_name : `str`
        The name of the `transformers` model to use. Should be the same as the corresponding
        `PretrainedTransformerMismatchedIndexer`.
    max_length : `int`, optional (default = `None`)
        If positive, folds input token IDs into multiple segments of this length, pass them
        through the transformer model independently, and concatenate the final representations.
        Should be set to the same value as the `max_length` option on the
        `PretrainedTransformerMismatchedIndexer`.
    train_parameters: `bool`, optional (default = `True`)
        If this is `True`, the transformer weights get updated during training.
    last_layer_only: `bool`, optional (default = `True`)
        When `True` (the default), only the final layer of the pretrained transformer is taken
        for the embeddings. But if set to `False`, a scalar mix of all of the layers
        is used.
    gradient_checkpointing: `bool`, optional (default = `None`)
        Enable or disable gradient checkpointing.
    tokenizer_kwargs: `Dict[str, Any]`, optional (default = `None`)
        Dictionary with
        [additional arguments](https://github.com/huggingface/transformers/blob/155c782a2ccd103cf63ad48a2becd7c76a7d2115/transformers/tokenization_utils.py#L691)
        for `AutoTokenizer.from_pretrained`.
    transformer_kwargs: `Dict[str, Any]`, optional (default = `None`)
        Dictionary with
        [additional arguments](https://github.com/huggingface/transformers/blob/155c782a2ccd103cf63ad48a2becd7c76a7d2115/transformers/modeling_utils.py#L253)
        for `AutoModel.from_pretrained`.
    sub_token_mode: `Optional[str]`, optional (default= `avg`)
        If `sub_token_mode` is set to `first`, return first sub-token representation as word-level representation
        If `sub_token_mode` is set to `avg`, return average of all the sub-tokens representation as word-level representation
        If `sub_token_mode` is not specified it defaults to `avg`
        If invalid `sub_token_mode` is provided, throw `ValueError`

    """  # noqa: E501

    def __init__(
        self,
        model_name: str,
        max_length: int = None,
        train_parameters: bool = True,
        last_layer_only: bool = True,
        gradient_checkpointing: Optional[bool] = None,
        tokenizer_kwargs: Optional[Dict[str, Any]] = None,
        transformer_kwargs: Optional[Dict[str, Any]] = None,
        sub_token_mode: Optional[str] = "avg",
    ) -> None:
        super().__init__()
        # The matched version v.s. mismatched
        self._matched_embedder = PretrainedTransformerEmbedder(
            model_name,
            max_length=max_length,
            train_parameters=train_parameters,
            last_layer_only=last_layer_only,
            gradient_checkpointing=gradient_checkpointing,
            tokenizer_kwargs=tokenizer_kwargs,
            transformer_kwargs=transformer_kwargs,
        )
        self.sub_token_mode = sub_token_mode

    @overrides
    def get_output_dim(self):
        return self._matched_embedder.get_output_dim()

    @overrides
    def forward(
        self,
        token_ids: torch.LongTensor,
        mask: torch.BoolTensor,
        offsets: torch.LongTensor,
        wordpiece_mask: torch.BoolTensor,
        type_ids: Optional[torch.LongTensor] = None,
        segment_concat_mask: Optional[torch.BoolTensor] = None,
    ) -> torch.Tensor:  # type: ignore
        """
        # Parameters

        token_ids: `torch.LongTensor`
            Shape: [batch_size, num_wordpieces] (for exception see `PretrainedTransformerEmbedder`).
        mask: `torch.BoolTensor`
            Shape: [batch_size, num_orig_tokens].
        offsets: `torch.LongTensor`
            Shape: [batch_size, num_orig_tokens, 2].
            Maps indices for the original tokens, i.e. those given as input to the indexer,
            to a span in token_ids. `token_ids[i][offsets[i][j][0]:offsets[i][j][1] + 1]`
            corresponds to the original j-th token from the i-th batch.
        wordpiece_mask: `torch.BoolTensor`
            Shape: [batch_size, num_wordpieces].
        type_ids: `Optional[torch.LongTensor]`
            Shape: [batch_size, num_wordpieces].
        segment_concat_mask: `Optional[torch.BoolTensor]`
            See `PretrainedTransformerEmbedder`.

        # Returns

        `torch.Tensor`
            Shape: [batch_size, num_orig_tokens, embedding_size].
        """
        # Shape: [batch_size, num_wordpieces, embedding_size].
        embeddings = self._matched_embedder(
            token_ids, wordpiece_mask, type_ids=type_ids, segment_concat_mask=segment_concat_mask
        )

        # span_embeddings: (batch_size, num_orig_tokens, max_span_length, embedding_size)
        # span_mask: (batch_size, num_orig_tokens, max_span_length)
        span_embeddings, span_mask = util.batched_span_select(embeddings.contiguous(), offsets)

        # int: maximum number of sub-tokens a batch has
        max_span_length = span_mask.shape[-1]

        # If "sub_token_mode" is set to "first", return the first sub-token embedding
        if self.sub_token_mode == "first":
            # Shape: (batch_size, num_orig_tokens, max_span_length)
            span_mask_bool = torch.zeros(span_mask.shape, dtype=torch.bool)

            # Shape: 1-dimensional tensor
            max_span_range_indices = torch.arange(max_span_length, dtype=torch.long).view(1, 1, -1)

            # Shape (batch_size, num_orig_tokens, max_span_length)
            span_mask = max_span_range_indices <= span_mask_bool
            span_mask = span_mask.unsqueeze(-1)

            # Shape: (batch_size, num_orig_tokens, max_span_length, embedding_size)
            span_embeddings *= span_mask

            # Sum over embeddings of all sub-tokens of a word where except first sub-token,
            # rest all sub-token's embeddings is zero padded
            # Shape: (batch_size, num_orig_tokens, embedding_size)
            orig_embeddings = span_embeddings.sum(2)

        # If "sub_token_mode" is set to "avg", return the average of embeddings of all sub-tokens of a word
        elif self.sub_token_mode == "avg":
            span_mask = span_mask.unsqueeze(-1)

            # Shape: (batch_size, num_orig_tokens, max_span_length, embedding_size)
            span_embeddings *= span_mask  # zero out paddings

            # Sum over embeddings of all sub-tokens of a word
            # Shape: (batch_size, num_orig_tokens, embedding_size)
            span_embeddings_sum = span_embeddings.sum(2)

            # Shape (batch_size, num_orig_tokens)
            span_embeddings_len = span_mask.sum(2)

            # Find the average of sub-tokens embeddings by dividing `span_embedding_sum` by `span_embedding_len`
            # Shape: (batch_size, num_orig_tokens, embedding_size)
            orig_embeddings = span_embeddings_sum / torch.clamp_min(span_embeddings_len, 1)

            # All the places where the span length is zero, write in zeros.
            orig_embeddings[(span_embeddings_len == 0).expand(orig_embeddings.shape)] = 0

        # If invalid "sub_token_mode" is provided, throw error
        else:
            raise ValueError("Do not recognise 'sub_token_mode' {}".format(self.sub_token_mode))

        return orig_embeddings
