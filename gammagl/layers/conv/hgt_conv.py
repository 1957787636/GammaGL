import math
import tensorlayerx as tlx
from gammagl.layers.conv import MessagePassing
from tensorlayerx.nn import ModuleDict, Linear, Parameter, ParameterDict
from gammagl.utils import segment_softmax


class HGTConv(MessagePassing):
    r"""The Heterogeneous Graph Transformer (HGT) operator from the
    `"Heterogeneous Graph Transformer" <https://arxiv.org/abs/2003.01332>`_
    paper.

    Args:
        in_channels (int or Dict[str, int]): Size of each input sample of every
            node type, or :obj:`-1` to derive the size from the first input(s)
            to the forward method.
        out_channels (int): Size of each output sample.
        metadata (Tuple[List[str], List[Tuple[str, str, str]]]): The metadata
            of the heterogeneous graph, *i.e.* its node and edge types given
            by a list of strings and a list of string triplets, respectively.
            See :meth:`torch_geometric.data.HeteroData.metadata` for more
            information.
        heads (int, optional): Number of multi-head-attentions.
            (default: :obj:`1`)
        group (string, optional): The aggregation scheme to use for grouping
            node embeddings generated by different relations.
            (:obj:`"sum"`, :obj:`"mean"`, :obj:`"min"`, :obj:`"max"`).
            (default: :obj:`"sum"`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """

    def __init__(
            self,
            in_channels,
            out_channels,
            metadata,
            heads: int = 1,
            group: str = "sum",
            dropout_rate=0,
    ):
        super().__init__()

        if not isinstance(in_channels, dict):
            in_channels = {node_type: in_channels for node_type in metadata[0]}

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.heads = heads
        self.group = group

        self.k_lin = ModuleDict()
        self.q_lin = ModuleDict()
        self.v_lin = ModuleDict()
        self.a_lin = ModuleDict()
        self.skip = ParameterDict()
        self.dropout_rate = dropout_rate
        self.dropout = tlx.layers.Dropout(self.dropout_rate)
        for node_type, in_channels in self.in_channels.items():
            self.k_lin[node_type] = Linear(in_features=in_channels, out_features=out_channels, act='relu6')
            self.q_lin[node_type] = Linear(in_features=in_channels, out_features=out_channels, act='relu6')
            self.v_lin[node_type] = Linear(in_features=in_channels, out_features=out_channels, act='relu6')
            self.a_lin[node_type] = Linear(in_features=out_channels, out_features=out_channels, act='relu6')
            self.skip[node_type] = Parameter(data=tlx.ops.convert_to_tensor(1.0))

        self.a_rel = ParameterDict()
        self.m_rel = ParameterDict()
        self.p_rel = ParameterDict()
        dim = out_channels // heads
        init_a = tlx.initializers.TruncatedNormal()
        init_m = tlx.initializers.TruncatedNormal()
        for edge_type in metadata[1]:
            edge_type = '__'.join(edge_type)
            self.a_rel[edge_type + 'a'] = self._get_weights(edge_type + 'a', shape=(heads, dim, dim), init=init_a,
                                                            order=True)
            self.m_rel[edge_type + 'm'] = self._get_weights(edge_type + 'm', shape=(heads, dim, dim), init=init_m,
                                                            order=True)
            self.p_rel[edge_type] = Parameter(tlx.ones(shape=(heads,)))

    def forward(self, x_dict, edge_index_dict):

        H, D = self.heads, self.out_channels // self.heads
        k_dict, q_dict, v_dict, out_dict = {}, {}, {}, {}

        # Iterate over node-types:
        for node_type, x in x_dict.items():
            k_dict[node_type] = tlx.ops.reshape(self.k_lin[node_type](x), (-1, H, D))
            q_dict[node_type] = tlx.ops.reshape(self.q_lin[node_type](x), (-1, H, D))
            v_dict[node_type] = tlx.ops.reshape(self.v_lin[node_type](x), (-1, H, D))
            out_dict[node_type] = []

        # Iterate over edge-types:
        for edge_type, edge_index in edge_index_dict.items():
            transpose = tlx.nn.Transpose([1, 0, 2])
            src_type, _, dst_type = edge_type
            edge_type = '__'.join(edge_type)

            a_rel = self.a_rel[edge_type + 'a']
            k = transpose((transpose(k_dict[src_type]) @ a_rel))

            m_rel = self.m_rel[edge_type + 'm']
            v = transpose((transpose(v_dict[src_type]) @ m_rel))

            if tlx.BACKEND != 'tensorflow':
                edge_index = tlx.ops.convert_to_tensor(edge_index, dtype='int64')
            source_index, target_index = edge_index[0], edge_index[1]
            q_i = tlx.gather(q_dict[dst_type], target_index, axis=0)
            v_j = tlx.gather(v, source_index, axis=0)
            k_j = tlx.gather(k, source_index, axis=0)
            rel = self.p_rel[edge_type]
            out = self.propagate(edge_index=edge_index, aggr='sum', q_i=q_i, k_j=k_j, v_j=v_j, rel=rel,
                                 num_nodes=x_dict[dst_type].shape[0])
            out_dict[dst_type].append(out)

        # Iterate over node-types:
        for node_type, outs in out_dict.items():
            outs = tlx.stack(outs)
            out = tlx.ops.reduce_sum(outs, axis=0, keepdims=False)
            out = self.a_lin[node_type](out)
            alpha = tlx.ops.sigmoid(self.skip[node_type])
            out = alpha * out + (1 - alpha) * x_dict[node_type]
            out_dict[node_type] = out

        return out_dict

    def propagate(self, edge_index, aggr='sum', **kwargs):
        coll_dict = {}
        for k, v in kwargs.items():
            coll_dict[k] = v
        coll_dict['edge_index'] = edge_index
        coll_dict['aggr'] = aggr
        coll_dict['target_index'] = edge_index[1]
        msg_kwargs = self.inspector.distribute('message', coll_dict)
        msg = self.message(**msg_kwargs)
        x = self.aggregate(msg, edge_index, num_nodes=kwargs['num_nodes'], aggr=aggr)
        x = self.update(x)
        return x

    def message(self, k_j, q_i, v_j, rel, target_index, num_nodes):
        alpha = tlx.ops.reduce_sum(k_j * q_i, axis=-1, keepdims=False)
        alpha = alpha * rel
        alpha = alpha / math.sqrt(q_i.shape[-1])
        alpha = self.dropout(segment_softmax(alpha, target_index, num_nodes))
        out = v_j * tlx.expand_dims(alpha, -1)
        return tlx.ops.reshape(out, (-1, self.out_channels))

    def aggregate(self, msg, edge_index, num_nodes=None, aggr='sum'):
        return super().aggregate(msg, edge_index, num_nodes, aggr)

    def update(self, x):
        return super().update(x)
