from typing import Dict, Union

from overrides import overrides
import torch
from torch import nn

from allennlp.data.vocabulary import Vocabulary
from allennlp.models.model import Model
from myallennlp.modules.token_embedders.layered_bert_token_embedder import LayeredPretrainedBertModel
from myallennlp.pytorch_pretrained_bert.modeling import LayeredBertModel
from allennlp.nn.initializers import InitializerApplicator
from allennlp.training.metrics import CategoricalAccuracy
from allennlp.models.archival import load_archive


@Model.register("multi_layered_bert_for_classification")
class MultiLayeredBertForClassification(Model):
    """
    An AllenNLP Model that runs pretrained BERT,
    takes the pooled output, and adds a Linear layer on top.
    If you want an easy way to use BERT for classification, this is it.
    Note that this is a somewhat non-AllenNLP-ish model architecture,
    in that it essentially requires you to use the "bert-pretrained"
    token indexer, rather than configuring whatever indexing scheme you like.

    See `allennlp/tests/fixtures/bert/bert_for_classification.jsonnet`
    for an example of what your config might look like.

    Parameters
    ----------
    vocab : ``Vocabulary``
    bert_model : ``Union[str, BertModel]``
        The BERT model to be wrapped. If a string is provided, we will call
        ``BertModel.from_pretrained(bert_model)`` and use the result.
    num_labels : ``int``, optional (default: None)
        How many output classes to predict. If not provided, we'll use the
        vocab_size for the ``label_namespace``.
    index : ``str``, optional (default: "bert")
        The index of the token indexer that generates the BERT indices.
    label_namespace : ``str``, optional (default : "labels")
        Used to determine the number of classes if ``num_labels`` is not supplied.
    trainable : ``bool``, optional (default : True)
        If True, the weights of the pretrained BERT model will be updated during training.
        Otherwise, they will be frozen and only the final linear layer will be trained.
    scaling_temperatures: ``str``, optional (default: "1")
        Scaling temperature parameter of each layer for better calibration
    linear_layers: ``str``, optional (default: None)
        files containing linear layers to load
    initializer : ``InitializerApplicator``, optional
        If provided, will be used to initialize the final linear layer *only*.
    """
    def __init__(self,
                 vocab: Vocabulary,
                 bert_model: Union[str, LayeredPretrainedBertModel],
                 dropout: float = 0.0,
                 num_labels: int = None,
                 index: str = "bert",
                 label_namespace: str = "labels",
                 trainable: bool = True,
                 num_predicted_hidden_layers: int = -1,
                 scaling_temperature: str = "1",
                 temperature_threshold: float = -1,
                 layer_index: str = "-1",
                 linear_layers: str = None,
                 initializer: InitializerApplicator = InitializerApplicator()) -> None:
        super().__init__(vocab)

        if isinstance(bert_model, str):
            self.bert_model = LayeredPretrainedBertModel.load(bert_model)
        else:
            self.bert_model = bert_model

        for param in self.bert_model.parameters():
            param.requires_grad = trainable

#        self.bert_model.requires_grad = trainable

        in_features = self.bert_model.config.hidden_size

        if num_labels:
            out_features = num_labels
        else:
            out_features = vocab.get_vocab_size(label_namespace)

        self._dropout = torch.nn.Dropout(p=dropout)

        self._layer_indices = [int(x) for x in layer_index.split(",")]
        self._classification_layers = nn.ModuleList([torch.nn.Linear(in_features, out_features) for i in self._layer_indices])
        self._sum_weights = torch.nn.parameter(torch.randn(len(self._layer_indices)))

        normalize(self._sum_weights)

        if linear_layers is not None:
            linear_layers = linear_layers.split(",")

            for (i, f) in enumerate(linear_layers):
                self._classification_layers[i] = torch.load(f)


        self._accuracy = CategoricalAccuracy()
        self._loss = torch.nn.CrossEntropyLoss()
        self._index = index
        self._num_predicted_hidden_layers = num_predicted_hidden_layers
        self._scaling_temperatures = [float(x) for x in scaling_temperature.split(",")]
        self._trainable = trainable
        self._temperature_threshold = temperature_threshold

        for l in self._classification_layers:
            initializer(l)

    def normalize(vec):
        data[i] = vec / torch.norm(vec)  # unit length

    def forward(self,  # type: ignore
                tokens: Dict[str, torch.LongTensor],
                label: torch.IntTensor = None) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        tokens : Dict[str, torch.LongTensor]
            From a ``TextField`` (that has a bert-pretrained token indexer)
        label : torch.IntTensor, optional (default = None)
            From a ``LabelField``

        Returns
        -------
        An output dictionary consisting of:

        logits : torch.FloatTensor
            A tensor of shape ``(batch_size, num_labels)`` representing
            unnormalized log probabilities of the label.
        probs : torch.FloatTensor
            A tensor of shape ``(batch_size, num_labels)`` representing
            probabilities of the label.
        loss : torch.FloatTensor, optional
            A scalar loss to be optimised.
        """
        input_ids = tokens[self._index]
        token_type_ids = tokens[f"{self._index}-type-ids"]
        input_mask = (input_ids != 0).long()

        stop, encoded_layer, output_dict = self._run_layer(input_ids, token_type_ids, input_mask, label, 0, 0,
                                                     None)

        if not stop:
            for i in range(len(self._layer_indices) - 1):
                stop, encoded_layer, output_dict = self._run_layer(input_ids, token_type_ids, input_mask, label, i+1,
                                                             self._layer_indices[i]+1, encoded_layer)

                if stop:
                    break

        if label is not None:
            logits = output_dict["logits"]
            loss = self._loss(logits, label.long().view(-1))
            output_dict["loss"] = loss
            self._accuracy(logits, label)

        normalize(self._sum_weights)

        return output_dict


    def _run_layer(self, input_ids, token_type_ids, input_mask, label, layer_index, start_index, previous_layer):
        """Run model on a single layer"""
        encoded_layer, pooled = self.bert_model(input_ids=input_ids,
                                                token_type_ids=token_type_ids,
                                                attention_mask=input_mask,
                                                output_all_encoded_layers=False,
                                                layer_index=self._layer_indices[layer_index],
                                                num_predicted_hidden_layers=self._layer_indices[layer_index],
                                                start_index=start_index, previous_layer=previous_layer
                                                )

        pooled = self._dropout(pooled)

        pooled = torch.sum(self._sum_weights * pooled)

        # apply classification layer
        logits = self._classification_layers[layer_index](pooled)

        probs = torch.nn.functional.softmax(logits / self._scaling_temperatures[layer_index], dim=-1)

        output_dict = {"logits": logits, "probs": probs, "selected_layer": torch.FloatTensor([self._layer_indices[layer_index]]).cuda(), "correct_label": label}

        is_done = not self._trainable and torch.max(probs) >= self._temperature_threshold

        return is_done, encoded_layer, output_dict



    @overrides
    def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        Does a simple argmax over the probabilities, converts index to string label, and
        add ``"label"`` key to the dictionary with the result.
        """
        predictions = output_dict["probs"]
        if predictions.dim() == 2:
            predictions_list = [predictions[i] for i in range(predictions.shape[0])]
        else:
            predictions_list = [predictions]
        classes = []
        for prediction in predictions_list:
            label_idx = prediction.argmax(dim=-1).item()
            label_str = self.vocab.get_token_from_index(label_idx, namespace="labels")
            classes.append(label_str)
        output_dict["label"] = classes
        return output_dict

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        metrics = {'accuracy': self._accuracy.get_metric(reset)}
        return metrics
