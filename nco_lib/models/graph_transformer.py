import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from abc import abstractmethod
from .base_layers import MLP, Norm
from .decoders import DECODER_DICT


class BaseGTEncoderLayer(nn.Module):
    def __init__(self, hidden_dim: int, mult_hidden: int, n_heads: int, dropout: float, activation: str,
                 normalization: str, bias: bool = False, aux_node: bool = False):
        """
        Base Graph Transformer layer class.
        :param hidden_dim: int: The input dimension of the layer.
        :param mult_hidden: int: The multiplier for the hidden dimension of the MLP.
        :param n_heads: int: The number of attention heads.
        :param dropout: float: The dropout rate.
        :param activation: str: The activation function to use in the MLP.
        :param normalization: str: The normalization to use.
        :param bias: bool: Whether to use bias in the linear layers.
        :param aux_node: bool: Whether to use an auxiliary (also known as virtual) node.
        """
        super(BaseGTEncoderLayer, self).__init__()
        self.hidden_dim = hidden_dim
        self.n_heads = n_heads
        self.head_dim = hidden_dim // n_heads
        self.dropout = dropout
        self.aux_node = aux_node

        self.norm1 = Norm(hidden_dim=hidden_dim, normalization=normalization)
        self.norm2 = Norm(hidden_dim=hidden_dim, normalization=normalization)
        self.mlp = MLP(hidden_dim=hidden_dim, mult_hidden=mult_hidden, activation=activation, dropout=dropout, bias=bias)

    def forward(self, *args, **kwargs):
        raise NotImplementedError("Forward method not implemented")


class GTEncoderLayer(BaseGTEncoderLayer):
    def __init__(self, hidden_dim: int, mult_hidden: int, n_heads: int, dropout: float, activation: str,
                 normalization: str, bias: bool = False):
        super(GTEncoderLayer, self).__init__(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization, bias)
        self.W_h = nn.Linear(hidden_dim, 3 * hidden_dim, bias=bias)  # Linear transformation for q, k, v

    def forward(self, h):
        """
        Forward pass for the GTLayer.
        :param h: torch.Tensor: The node embeddings. Shape: (batch_size, n_nodes, hidden_dim).
        """
        batch_size, n_nodes, _ = h.shape
        h_in = h.clone()

        # Initial normalization
        h = self.norm1(h)

        # Linear transformation
        q, k, v = self.W_h(h).split(self.hidden_dim, dim=2)
        k = k.view(batch_size, n_nodes, self.n_heads, self.head_dim).transpose(1, 2)  # (B, nh, T, hs)
        q = q.view(batch_size, n_nodes, self.n_heads, self.head_dim).transpose(1, 2)  # (B, nh, T, hs)
        v = v.view(batch_size, n_nodes, self.n_heads, self.head_dim).transpose(1, 2)  # (B, nh, T, hs)

        # Attention mechanism
        y = torch.nn.functional.scaled_dot_product_attention(q, k, v, dropout_p=self.dropout if self.training else 0)
        y = y.transpose(1, 2).contiguous().view(batch_size, n_nodes, self.hidden_dim)
        # all nan values are replaced with 0
        y = torch.where(torch.isnan(y), torch.zeros_like(y), y)

        # Add residual, Normalization and MLP
        out = self.mlp(self.norm2(y + h_in))

        # Final residual connection
        return out + y


class EdgeGTEncoderLayer(BaseGTEncoderLayer):
    def __init__(self, hidden_dim: int, mult_hidden: int, n_heads: int, dropout: float, activation: str,
                 normalization: str, bias: bool = False):
        super(EdgeGTEncoderLayer, self).__init__(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization,
                                                 bias)
        self.W_h = nn.Linear(hidden_dim, 3 * hidden_dim, bias=bias)  # Linear transformation for q, k, v
        self.W_e = nn.Linear(hidden_dim, 2 * n_heads, bias=bias)  # Additional edge weights

    def forward(self, h: torch.Tensor, e: torch.Tensor):
        """
        Forward pass for the EdgeGTLayer.
        :param h: torch.Tensor: The node embeddings. Shape: (batch_size, n_nodes, hidden_dim).
        :param e: torch.Tensor: The edge embeddings. Shape: (batch_size, n_nodes, n_nodes, hidden_dim).
        """
        batch_size, n_nodes, _ = h.shape
        h_in = h.clone()

        # Initial normalization
        h = self.norm1(h)

        # Linear transformation for node embeddings
        q, k, v = self.W_h(h).split(self.hidden_dim, dim=2)
        k = k.view(batch_size, n_nodes, self.n_heads, self.head_dim).transpose(1, 2)  # (B, nh, T, hs)
        q = q.view(batch_size, n_nodes, self.n_heads, self.head_dim).transpose(1, 2)  # (B, nh, T, hs)
        v = v.view(batch_size, n_nodes, self.n_heads, self.head_dim).transpose(1, 2)  # (B, nh, T, hs)

        # Linear transformation for edge embeddings
        e1, e2 = self.W_e(e).split(self.n_heads, dim=3)
        e1 = e1.transpose(2, 3).transpose(1, 2)  # (B, nh, T, T)
        e2 = e2.transpose(2, 3).transpose(1, 2)  # (B, nh, T, T)

        # Attention mechanism
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att + e1  # Add edge weights to attention scores
        att = F.softmax(att, dim=-1)
        att = att * e2  # Multiply edge weights with attention scores
        y = att @ v  # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).reshape(batch_size, n_nodes, self.hidden_dim)

        # Add residual, Normalization and MLP
        out = self.mlp(self.norm2(y + h_in))

        # Final residual connection
        return out + y

class DeepMCGCN(BaseGTModel):
    def __init__(self, node_in_dim: int, edge_in_dim: int, node_out_dim: int = 1, decoder: str = 'linear',
                 hidden_dim: int = 128, n_encoder_layers: int = 3, mult_hidden: int = 4, n_heads: int = 8,
                 dropout: float = 0.0, activation: str = 'relu', normalization: str = 'layer', bias: bool = False,
                 logit_clipping: float = 10.0):
        """
        Node- and Edge-based featured Graph Transformer model class with node-based action outputs. It processes the first half
        edge features independently from the second half edge features. That is, it assumes that the input consists of two graphs 
        with edge_in_dim/2 edge features each that have to be processed independently.

        :param node_in_dim: int: The input dimension of the node features.
        :param edge_in_dim: int: The input dimension of the edge features.
        :param node_out_dim: int: The output dimension of the node-based action logits.
        :param decoder: str: The decoder to use. Options: 'linear', 'attention'.
        :param hidden_dim: int: The hidden dimension of the model.
        :param n_encoder_layers: int: The number of layers in the model.
        :param mult_hidden: int: The multiplier for the hidden dimension of the MLP.
        :param n_heads: int: The number of attention heads.
        :param dropout: float: The dropout rate.
        :param activation: str: The activation function to use in the MLP.
        :param normalization: str: The normalization to use.
        :param bias: bool: Whether to use bias in the linear layers.
        :param logit_clipping: float: The logit clipping value. 0.0 means no clipping. 10.0 is a commonly used value.
        """
        super(DeepMCGCN, self).__init__(out_dim=node_out_dim, hidden_dim=hidden_dim, logit_clipping=logit_clipping)
        self.node_in_dim = node_in_dim
        self.node_out_dim = node_out_dim
        self.edge_in_dim = edge_in_dim

        self.in_node_projection = nn.Linear(self.node_in_dim, hidden_dim, bias=bias)
        self.in_node_projection_1 = nn.Linear(self.node_in_dim, hidden_dim, bias=bias)
        self.in_node_projection_2 = nn.Linear(self.node_in_dim, hidden_dim, bias=bias)

        self.in_edge_projection = nn.Linear(int(self.edge_in_dim/2), hidden_dim, bias=bias)
        self.in_edge_projection_1 = nn.Linear(int(self.edge_in_dim/2), hidden_dim, bias=bias)
        self.in_edge_projection_2 = nn.Linear(int(self.edge_in_dim/2), hidden_dim, bias=bias)

        self.encoder_layers = nn.ModuleList([EdgeGTEncoderLayer(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization, bias)
                                            for _ in range(n_encoder_layers)])
        self.encoder_layers_1 = nn.ModuleList([EdgeGTEncoderLayer(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization, bias)
                                            for _ in range(n_encoder_layers)])
        self.encoder_layers_2 = nn.ModuleList([EdgeGTEncoderLayer(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization, bias)
                                            for _ in range(n_encoder_layers)])

        self.mlp = MLP(hidden_dim=3*hidden_dim, mult_hidden=1, activation=activation, dropout=dropout, bias=bias)
        self.out_node_projection = nn.Linear(3*hidden_dim, hidden_dim, bias=bias)

        assert decoder in DECODER_DICT.keys(), f"Decoder must be one of {DECODER_DICT.keys()}"
        self.decoder = DECODER_DICT[decoder](hidden_dim, node_out_dim, n_heads, False, bias=bias)

    def forward(self, state):
        # Reshape the node features to (batch_size * pomo_size, n_nodes, features)
        node_features = state.node_features.clone().view(state.batch_size*state.pomo_size, state.problem_size, -1)

        # Add memory information to node features
        if state.memory_info is not None:
            memory = state.memory_info.clone().view(state.batch_size*state.pomo_size, state.problem_size, -1)
            node_features = torch.cat([node_features, memory], dim=-1)

        # Edge features
        edges = state.edge_features.clone().view(state.batch_size*state.pomo_size, state.problem_size, state.problem_size, -1)

        #Combined channel (graph)
        edge_feat = edges[:,:,:,:int(self.edge_in_dim/2)] + edges[:,:,:,int(self.edge_in_dim/2):]
        #First channel (graph)
        edge_feat_1 = edges[:,:,:,:int(self.edge_in_dim/2)]
        #Second channel (graph)
        edge_feat_2 = edges[:,:,:,int(self.edge_in_dim/2):]

        # Initial projection from node features to node embeddings

        #Combined channel (graph)
        h = self.in_node_projection(node_features)
        #First channel (graph)
        h1 = self.in_node_projection_1(node_features)
        #Second channel (graph)
        h2 = self.in_node_projection_2(node_features)

        # Initial projection from edge features to edge embeddings
        
        #Combined channel (graph)
        e = self.in_edge_projection(edge_feat)
        #First channel (graph)
        e1 = self.in_edge_projection_1(edge_feat_1)
        #Second channel (graph)
        e2 = self.in_edge_projection_2(edge_feat_2)

        # Pass through the layers

        #Initialize residual connections
        h_res = torch.clone(h)
        h1_res = torch.clone(h1)
        h2_res = torch.clone(h2)
        
        #Parallel encoders phase
        for i,_ in enumerate(self.encoder_layers):

            #Process encoder layer
            h = self.encoder_layers[i](h, e)
            h1 = self.encoder_layers_1[i](h1, e1)
            h2 = self.encoder_layers_2[i](h2, e2)

            #Add connections between enconders + residual connection
            h_ = h + h1 + h2 + h_res
            h1_ = h1 + h2 + h1_res
            h2_ = h1 + h2 + h2_res

            h = h_
            h1 = h1_
            h2 = h2_

            #Update residual connections
            h_res = torch.clone(h)
            h1_res = torch.clone(h1)
            h2_res = torch.clone(h2)
        
        # MLP that joins the information from all channels
        h = self.mlp(torch.cat([h, h1, h2], dim=-1))
        h = self.out_node_projection(h)

        # Decode to node-based action logits
        out, aux_node = self.decoder(h)

        if self.clip_logits:
            out = out / self.sqrt_embedding_dim
            out = self.logit_clipping * torch.tanh(out)

        return out, aux_node


class BaseGTModel(nn.Module):
    def __init__(self, out_dim: int, hidden_dim: int, logit_clipping: float):
        """
        Base Graph Transformer model class.
        """
        super(BaseGTModel, self).__init__()
        self.out_dim = out_dim
        self.sqrt_embedding_dim = math.sqrt(hidden_dim)

        self.clip_logits = logit_clipping > 0.0
        self.logit_clipping = logit_clipping

    @abstractmethod
    def forward(self, *inputs):
        pass


class GTModel(BaseGTModel):
    def __init__(self, node_in_dim: int, node_out_dim: int = 1, decoder: str = 'linear', hidden_dim: int = 128, n_encoder_layers: int = 3,
                 mult_hidden: int = 4, n_heads: int = 8, dropout: float = 0.0, activation: str = 'relu',
                 normalization: str = 'layer', bias: bool = False, aux_node: bool = False, logit_clipping: float = 10.0):
        """
        Node-based featured Graph Transformer model class with node-based action outputs.

        :param node_in_dim: int: The input dimension of the node features.
        :param node_out_dim: int: The output dimension of the node-based action logits.
        :param decoder: str: The decoder to use. Options: 'linear', 'attention'.
        :param hidden_dim: int: The hidden dimension of the model.
        :param n_encoder_layers: int: The number of layers in the model.
        :param mult_hidden: int: The multiplier for the hidden dimension of the MLP.
        :param n_heads: int: The number of attention heads.
        :param dropout: float: The dropout rate.
        :param activation: str: The activation function to use in the MLP.
        :param normalization: str: The normalization to use.
        :param bias: bool: Whether to use bias in the linear layers.
        :param aux_node: bool: Whether to use an auxiliary (also known as virtual) node.
        :param logit_clipping: float: The logit clipping value. 0.0 means no clipping. 10.0 is a commonly used value.
        """
        super(GTModel, self).__init__(out_dim=node_out_dim, hidden_dim=hidden_dim, logit_clipping=logit_clipping)
        self.node_in_dim = node_in_dim
        self.node_out_dim = node_out_dim

        self.aux_node = aux_node
        if aux_node:
            self.virtual_nodes = nn.Parameter(torch.randn(node_out_dim, hidden_dim))

        self.in_projection = nn.Linear(node_in_dim, hidden_dim, bias=bias)
        self.encoder_layers = nn.ModuleList([GTEncoderLayer(hidden_dim, mult_hidden, n_heads, dropout, activation,
                                                            normalization, bias) for _ in range(n_encoder_layers)])

        assert decoder in DECODER_DICT.keys(), f"Decoder must be one of {DECODER_DICT.keys()}"
        self.decoder = DECODER_DICT[decoder](hidden_dim, node_out_dim, n_heads, aux_node, bias=bias)

    def forward(self, state):
        """
        Forward pass for the GTModel.
        :param state: State: The state of the environment.
        """

        # Reshape the node features to (batch_size * pomo_size, n_nodes, features)
        node_features = state.node_features.clone().view(-1, state.node_features.size(2), state.node_features.size(3))

        # Add memory information to node features
        if state.memory_info is not None:
            memory = state.memory_info.clone().view(state.batch_size*state.pomo_size, state.problem_size, -1)
            node_features = torch.cat([node_features, memory], dim=-1)

        # Initial projection from node features to node embeddings
        h = self.in_projection(node_features)

        if self.aux_node:
            # Append virtual node
            virtual_node_features = self.virtual_nodes.unsqueeze(0).repeat(h.size(0), 1, 1)

            h = torch.cat([h, virtual_node_features], dim=1)

        # Pass through the encoding layers
        for layer in self.encoder_layers:
            h = layer(h)

        # Decode to node-based action logits
        out, aux_node = self.decoder(h)

        if self.clip_logits:
            out = out / self.sqrt_embedding_dim
            out = self.logit_clipping * torch.tanh(out)

        return out, aux_node


class EdgeInGTModel(BaseGTModel):
    def __init__(self, node_in_dim: int, edge_in_dim: int, node_out_dim: int = 1, decoder: str = 'linear',
                 hidden_dim: int = 128, n_encoder_layers: int = 3, mult_hidden: int = 4, n_heads: int = 8,
                 dropout: float = 0.0, activation: str = 'relu', normalization: str = 'layer', bias: bool = False,
                 aux_node: bool = False, logit_clipping: float = 10.0):
        """
        Node- and Edge-based featured Graph Transformer model class with node-based action outputs.

        :param node_in_dim: int: The input dimension of the node features.
        :param edge_in_dim: int: The input dimension of the edge features.
        :param node_out_dim: int: The output dimension of the node-based action logits.
        :param decoder: str: The decoder to use. Options: 'linear', 'attention'.
        :param hidden_dim: int: The hidden dimension of the model.
        :param n_encoder_layers: int: The number of layers in the model.
        :param mult_hidden: int: The multiplier for the hidden dimension of the MLP.
        :param n_heads: int: The number of attention heads.
        :param dropout: float: The dropout rate.
        :param activation: str: The activation function to use in the MLP.
        :param normalization: str: The normalization to use.
        :param bias: bool: Whether to use bias in the linear layers.
        :param aux_node: bool: Whether to use an auxiliary (also known as virtual) node.
        :param logit_clipping: float: The logit clipping value. 0.0 means no clipping. 10.0 is a commonly used value.
        """
        super(EdgeInGTModel, self).__init__(out_dim=node_out_dim, hidden_dim=hidden_dim, logit_clipping=logit_clipping)
        self.node_in_dim = node_in_dim
        self.node_out_dim = node_out_dim
        self.edge_in_dim = edge_in_dim

        self.aux_node = aux_node
        if aux_node:
            self.virtual_nodes = nn.Parameter(torch.randn(1, hidden_dim))

        self.in_node_projection = nn.Linear(node_in_dim, hidden_dim, bias=bias)
        self.in_edge_projection = nn.Linear(edge_in_dim, hidden_dim, bias=bias)
        self.encoder_layers = nn.ModuleList([EdgeGTEncoderLayer(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization, bias)
                                            for _ in range(n_encoder_layers)])

        assert decoder in DECODER_DICT.keys(), f"Decoder must be one of {DECODER_DICT.keys()}"
        self.decoder = DECODER_DICT[decoder](hidden_dim, node_out_dim, n_heads, aux_node, bias=bias)

    def forward(self, state):
        # Reshape the node features to (batch_size * pomo_size, n_nodes, features)
        node_features = state.node_features.clone().view(state.batch_size*state.pomo_size, state.problem_size, -1)

        # Add memory information to node features
        if state.memory_info is not None:
            memory = state.memory_info.clone().view(state.batch_size*state.pomo_size, state.problem_size, -1)
            node_features = torch.cat([node_features, memory], dim=-1)

        # Initial projection from node features to node embeddings
        h = self.in_node_projection(node_features)

        # Edge features
        edge_feat = state.edge_features.clone().view(state.batch_size*state.pomo_size, state.problem_size, state.problem_size, -1)
        if self.aux_node:
            # Append virtual node
            virtual_node_features = self.virtual_nodes.unsqueeze(0).repeat(h.size(0), 1, 1)
            h = torch.cat([h, virtual_node_features], dim=1)

            # Update adjacency matrix for virtual node

            virtual_edges = torch.ones(state.batch_size*state.pomo_size, 1, state.problem_size, self.edge_in_dim,  dtype=torch.long, device=state.device)
            edge_feat = torch.cat([edge_feat, virtual_edges], dim=1)
            virtual_edges_t = torch.ones(state.batch_size*state.pomo_size, state.problem_size + 1, 1, self.edge_in_dim, dtype=torch.long, device=state.device)
            edge_feat = torch.cat([edge_feat, virtual_edges_t], dim=2)

        # Initial projection from edge features to edge embeddings
        e = self.in_edge_projection(edge_feat)

        # Pass through the layers
        for layer in self.encoder_layers:
            h = layer(h, e)

        # Decode to node-based action logits
        out, aux_node = self.decoder(h)

        if self.clip_logits:
            out = out / self.sqrt_embedding_dim
            out = self.logit_clipping * torch.tanh(out)

        return out, aux_node


'''class EdgeOutGTModel(BaseGTModel):
    def __init__(self, node_in_dim: int, edge_out_dim: int = 1, hidden_dim: int = 128, n_layers: int = 3,
                 mult_hidden: int = 4, n_heads: int = 8, dropout: float = 0.0, activation: str = 'relu',
                 normalization: str = 'layer', bias: bool = False):
        """
        Node-based Graph Transformer model class with edge-based action outputs.

        :param node_in_dim: int: The input dimension of the node features.
        :param edge_out_dim: int: The output dimension of the edge-based action logits.
        :param hidden_dim: int: The hidden dimension of the model.
        :param n_layers: int: The number of layers in the model.
        :param mult_hidden: int: The multiplier for the hidden dimension of the MLP.
        :param n_heads: int: The number of attention heads.
        :param dropout: float: The dropout rate.
        :param activation: str: The activation function to use in the MLP.
        :param normalization: str: The normalization to use.
        :param bias: bool: Whether to use bias in the linear layers.
        """
        super(EdgeOutGTModel, self).__init__()
        self.in_node_projection = nn.Linear(node_in_dim, hidden_dim, bias=bias)
        self.layers = nn.ModuleList([EdgeGTEncoderLayer(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization, bias)
                                     for _ in range(n_layers)])
        self.out_projection = nn.Linear(2*hidden_dim, edge_out_dim, bias=bias)

    def forward(self, state):
        # Initial projection from node features to node embeddings
        h = self.in_node_projection(state.node_features)

        if self.aux_node:
            # Append virtual node
            virtual_node_features = self.virtual_nodes.unsqueeze(0).repeat(h.size(0), 1, 1)
            h = torch.cat([h, virtual_node_features], dim=1)

        # Pass through the layers
        for layer in self.layers:
            h = layer(h)

        # Get edge embedding e_ij by concatenating h_i and h_j
        e_out = torch.cat([h.unsqueeze(1).expand(-1, state.problem_size, -1, -1), h.unsqueeze(2).expand(-1, -1, state.problem_size, -1)], dim=-1)

        # Final projection to edge-based action logits
        logits = self.out_projection(e_out)
        return logits.reshape(state.batch_size, state.problem_size*state.problem_size, -1)
'''


class EdgeInOutGTModel(BaseGTModel):
    def __init__(self, node_in_dim: int, edge_in_dim: int, edge_out_dim: int = 1, decoder: str = 'linear',
                 hidden_dim: int = 128, n_encoder_layers: int = 3, mult_hidden: int = 4, n_heads: int = 8,
                 dropout: float = 0.0, activation: str = 'relu', normalization: str = 'layer', bias: bool = False,
                 aux_node: bool = False, logit_clipping: float = 10.0):
        """
        Node- and Edge-based Graph Transformer model class with edge-based action outputs.

        :param node_in_dim: int: The input dimension of the node features.
        :param edge_in_dim: int: The input dimension of the edge features.
        :param edge_out_dim: int: The output dimension of the edge-based action logits.
        :param decoder: str: The decoder to use. Options: 'linear', 'attention'.
        :param hidden_dim: int: The hidden dimension of the model.
        :param n_encoder_layers: int: The number of layers in the model.
        :param mult_hidden: int: The multiplier for the hidden dimension of the MLP.
        :param n_heads: int: The number of attention heads.
        :param dropout: float: The dropout rate.
        :param activation: str: The activation function to use in the MLP.
        :param normalization: str: The normalization to use.
        :param bias: bool: Whether to use bias in the linear layers.
        :param aux_node: bool: Whether to use an auxiliary (also known as virtual) node.
        :param logit_clipping: float: The logit clipping value. 0.0 means no clipping. 10.0 is a commonly used value.
        """
        super(EdgeInOutGTModel, self).__init__(out_dim=edge_out_dim, hidden_dim=hidden_dim, logit_clipping=logit_clipping)
        self.node_in_dim = node_in_dim
        self.edge_in_dim = edge_in_dim
        self.edge_out_dim = edge_out_dim
        self.aux_node = aux_node
        if aux_node:
            self.virtual_nodes = nn.Parameter(torch.randn(edge_out_dim, hidden_dim))

        self.in_node_projection = nn.Linear(node_in_dim, hidden_dim, bias=bias)
        self.in_edge_projection = nn.Linear(edge_in_dim, hidden_dim, bias=bias)
        self.encoder_layers = nn.ModuleList([EdgeGTEncoderLayer(hidden_dim, mult_hidden, n_heads, dropout, activation, normalization, bias)
                                             for _ in range(n_encoder_layers)])
        self.out_projection = nn.Linear(2*hidden_dim, edge_out_dim, bias=bias)
        assert decoder in DECODER_DICT.keys(), f"Decoder must be one of {DECODER_DICT.keys()}"
        if decoder in ['attention', 'linear']:
            print('Attention and Linear decoders are not supported for EdgeInOutGTModel. Using edge decoder instead.')
            decoder = 'edge'
        self.decoder = DECODER_DICT[decoder](2*hidden_dim, edge_out_dim, n_heads, aux_node, bias=bias)


    def forward(self, state):
        # Reshape the node features to (batch_size * pomo_size, n_nodes, features)
        node_features = state.node_features.clone().view(-1, state.node_features.size(2), state.node_features.size(3))

        # Add memory information to node features
        if state.memory_info is not None:
            memory = state.memory_info.clone().view(state.batch_size*state.pomo_size, state.problem_size, -1)
            node_features = torch.cat([node_features, memory], dim=-1)

        # Initial projection from node features to node embeddings
        h = self.in_node_projection(node_features)

        if self.aux_node:
            # Append virtual node
            virtual_node_features = self.virtual_nodes.unsqueeze(0).repeat(h.size(0), 1, 1)
            h = torch.cat([h, virtual_node_features], dim=1)

            # Update adjacency matrix for virtual node
            edge_feat = state.edge_features.clone()
            n_f = edge_feat.size(-1)
            virtual_edges = torch.ones(state.batch_size, state.pomo_size, 1, state.problem_size, n_f, dtype=torch.long, device=state.device)
            edge_feat = torch.cat([edge_feat, virtual_edges], dim=2)
            virtual_edges_t = torch.ones(state.batch_size, state.pomo_size, state.problem_size + 1, 1, n_f, dtype=torch.long, device=state.device)
            edge_feat = torch.cat([edge_feat, virtual_edges_t], dim=3)
        else:
            edge_feat = state.edge_features.clone()

        edge_feat = edge_feat.view(-1, edge_feat.size(2), edge_feat.size(3), edge_feat.size(4))

        # Initial projection from edge features to edge embeddings
        e = self.in_edge_projection(edge_feat)

        # Pass through the layers
        for layer in self.encoder_layers:
            h = layer(h, e)

        if self.aux_node:
            h, aux_node = h[:, :-1, :], h[:, -1:, :]
        else:
            aux_node = None

        # Get edge embedding e_ij by concatenating h_i and h_j
        e_out = torch.cat([h.unsqueeze(1).expand(-1, state.problem_size, -1, -1), h.unsqueeze(2).expand(-1, -1, state.problem_size, -1)], dim=-1)

        # reshape e_out to (batch_size, n_edges, hidden_dim)
        e_out = e_out.view(state.batch_size*state.pomo_size, -1, e_out.size(-1))

        # Decode to edge-based action logits
        out, _ = self.decoder(e_out)

        if self.clip_logits:
            out = out / self.sqrt_embedding_dim
            out = self.logit_clipping * torch.tanh(out)

        return out, aux_node
