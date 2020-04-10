from typing import Dict, Union, List

from overrides import overrides
import torch
import random

from allennlp.data.vocabulary import Vocabulary
from allennlp.models.model import Model
from myallennlp.modules.token_embedders.layered_bert_token_embedder import LayeredPretrainedBertModel
from allennlp.nn.initializers import InitializerApplicator
from allennlp.training.metrics import CategoricalAccuracy
from .multiloss_bert import MultilossBert

@Model.register("multiloss_bert_for_qa")
class MultilossBertForQuestionAnswering(MultilossBert):
    """
    Train a BERT model for question answering, which makes predictions based on multiple layers.

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
    layer_indices: ``str``, optional (default: "23")
        Indices for layers for which linear layers are learned
    multitask: ``bool``, optional (default: false)
        Do multitask learning (rather than summing all losses)
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
                 scaling_temperature: str = "1",
                 temperature_threshold: float = -1,
                 layer_indices: str = "23",
                 multitask: bool = False,
                 debug: bool = False,
                 add_previous_layer_logits: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator()) -> None:
        super().__init__(vocab, bert_model, dropout, num_labels, index, label_namespace, trainable, scaling_temperature, 
                        temperature_threshold, layer_indices, multitask, debug, add_previous_layer_logits, initializer)

        self._accuracy = CategoricalAccuracy()

        self._loss = torch.nn.CrossEntropyLoss()

        in_features = self.bert_model.config.hidden_size

        out_features = 2

        self._qa_outputs = torch.nn.ModuleList([torch.nn.Linear(in_features+(i*out_features*add_previous_layer_logits), out_features)
                                                            for i in range(len(self._layer_indices))])
        for l in self._qa_outputs:
            initializer(l)




    def forward(self,  # type: ignore
                input_ids: torch.Tensor,
                token_type_ids: torch.Tensor,
                attention_mask: torch.Tensor,
                tokens: List[str],
                document_tokens: List[str],
                token_to_original_map: Dict[int, int],
                token_is_max_context: Dict[int, bool]) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        start_logits, end_logits = self.bert_qa_model(torch.stack(input_ids),
                                                      torch.stack(token_type_ids),
                                                      torch.stack(attention_mask))
        output_dict = {"start_logits": start_logits,
                       "end_logits": end_logits,
                       "tokens": tokens,
                       "document_tokens": document_tokens,
                       "token_to_original_map": token_to_original_map,
                       "token_is_max_context": token_is_max_context}
        if self.training:
            loss = torch.sum(start_logits) * 0.0
            output_dict["loss"] = loss
        return output_dict



    # huggingface forward
    def qa_forward(self, input_ids, token_type_ids=None, attention_mask=None, start_positions=None, end_positions=None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            return total_loss
        else:
            return start_logits, end_logits



    # old textcat code
    def forward(self,  # type: ignore
                tokens: Dict[str, torch.LongTensor]) -> Dict[str, torch.Tensor]:
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

        output_dict = {}
        logit_list = [] 

        encoded_layer, previous_pooled = self._run_layer(input_ids, token_type_ids, input_mask, 0, 0, logit_list,
                                                     None, None)

        n_layers = len(self._layer_indices)

        if not self.training:
            logits = logit_list[0]
            probs = torch.nn.functional.softmax(logits, dim=-1)

            if torch.max(probs) >= self._temperature_threshold:
                n_layers = 1
#            print("li{}: logits={}, probs={}, thr={}".format(0, logits, probs, self._temperature_threshold))
        elif self._multitask:
            n_layers = random.randint(1,n_layers)

        for i in range(1, n_layers):
            encoded_layer, previous_pooled = self._run_layer(input_ids, token_type_ids, input_mask, i,
                                                 self._layer_indices[i-1]+1, logit_list, encoded_layer,
                                                 previous_pooled)

            if not self.training:
                logits = logit_list[i]
                probs = torch.nn.functional.softmax(logits, dim=-1)

#                print("li{}: logits={}, probs={}, thr={}".format(i, logits, probs, self._temperature_threshold))
                if torch.max(probs) >= self._temperature_threshold:
                    n_layers = i+1
                    break

        if not self.training:
            self._count_n_layers(n_layers)

        if label is not None:
            loss_list = []
            loss = None
            logits = None

            if self._multitask or n_layers == 1:
                logits = logit_list[-1] 
                loss = self._loss(logits, label.long().view(-1))
            else:
                for i in range(n_layers):
                    logits = logit_list[i]
                    loss = self._loss(logits, label.long().view(-1))
                    loss_list.append(loss)

            if not self.training and len(self._layer_indices) > 1 and self._debug:
                print("nl={}, loss_list={}".format(n_layers, loss_list))

            self._accuracy(logits, label)
            output_dict['probs'] = torch.nn.functional.softmax(logits, dim=-1)

            if self._multitask or n_layers == 1:
                output_dict['loss'] = loss
            else:
                output_dict['loss'] = torch.sum(torch.stack(loss_list, dim=0))

            output_dict["correct_label"] = label
            output_dict["n_layers"] = n_layers

        self._normalize_sum_weights()

        return output_dict


    def _run_layer(self, input_ids, token_type_ids, input_mask, layer_index, start_index, logit_list, previous_layer, previous_pooled):
        """Run model on a single layer"""
        encoded_layer, pooled = self.bert_model(input_ids=input_ids,
                                                token_type_ids=token_type_ids,
                                                attention_mask=input_mask,
                                                output_all_encoded_layers=True,
                                                layer_index=self._layer_indices[layer_index],
                                                num_predicted_hidden_layers=self._layer_indices[layer_index],
                                                start_index=start_index, previous_layer=previous_layer
                                                )

        pooled = self._dropout(pooled)

        if previous_pooled is not None:
            pooled = torch.cat([previous_pooled, pooled])

#        print("pooled={}, sw={}".format(pooled.size(), self._sum_weights[layer_index].size()))
        weighted_pooled = torch.einsum("a,abc->bc", (self._sum_weights[layer_index], pooled))

        if self._add_previous_layer_logits:
            weighted_pooled = torch.cat([weighted_pooled] + logit_list, dim=1)

        # apply classification layer
        logits = self._classification_layers[layer_index](weighted_pooled)/self._scaling_temperatures[layer_index]

        logit_list.append(logits)

        return encoded_layer[-1], pooled


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
        counts = self._count_n_layers.get_metric(reset) 
        metrics = {'accuracy': self._accuracy.get_metric(reset), 'thr': self._temperature_threshold}
        for i in range(len(self._layer_indices)):
           metrics['n_layers_'+str(i)] = counts[i]

        return metrics
