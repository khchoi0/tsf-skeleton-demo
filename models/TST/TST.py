# File modified from
# https://github.com/timeseriesAI/tsai/blob/main/tsai/models/TST.py

from .imports import *
from .layers import *


class _ScaledDotProductAttention(Module):
    def __init__(self, d_k: int):
        self.d_k = d_k

    def forward(
            self,
            q: Tensor,
            k: Tensor,
            v: Tensor,
            mask: Optional[Tensor] = None):
        # MatMul (q, k) - similarity scores for all pairs of positions in an
        # input sequence
        scores = torch.matmul(q, k)  # scores : [bs x n_heads x q_len x q_len]

        # Scale
        scores = scores / (self.d_k**0.5)

        # Mask (optional)
        if mask is not None:
            scores.masked_fill_(mask, -1e9)

        # SoftMax
        # attn   : [bs x n_heads x q_len x q_len]
        attn = F.softmax(scores, dim=-1)

        # MatMul (attn, v)
        # context: [bs x n_heads x q_len x d_v]
        context = torch.matmul(attn, v)

        return context, attn


# Internal Cell
class _MultiHeadAttention(Module):
    def __init__(self, d_model: int, n_heads: int, d_k: int, d_v: int):
        r"""
        Input shape:  Q, K, V:[batch_size (bs) x q_len x d_model], mask:[q_len x q_len]
        """
        self.n_heads, self.d_k, self.d_v = n_heads, d_k, d_v

        self.W_Q = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_K = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_V = nn.Linear(d_model, d_v * n_heads, bias=False)

        self.W_O = nn.Linear(n_heads * d_v, d_model, bias=False)

    def forward(
            self,
            Q: Tensor,
            K: Tensor,
            V: Tensor,
            mask: Optional[Tensor] = None):
        bs = Q.size(0)

        # Linear (+ split in multiple heads)
        q_s = (
            self.W_Q(Q).view(bs, -1, self.n_heads, self.d_k).transpose(1, 2)
        )  # q_s    : [bs x n_heads x q_len x d_k]
        k_s = (
            self.W_K(K).view(bs, -1, self.n_heads, self.d_k).permute(0, 2, 3, 1)
        )  # k_s    : [bs x n_heads x d_k x q_len] - transpose(1,2) + transpose(2,3)
        v_s = (
            self.W_V(V).view(bs, -1, self.n_heads, self.d_v).transpose(1, 2)
        )  # v_s    : [bs x n_heads x q_len x d_v]

        # Scaled Dot-Product Attention (multiple heads)
        # context: [bs x n_heads x q_len x d_v], attn: [bs x n_heads x q_len x
        # q_len]
        context, attn = _ScaledDotProductAttention(self.d_k)(q_s, k_s, v_s)

        # Concat
        context = (
            context.transpose(
                1, 2).contiguous().view(
                bs, -1, self.n_heads * self.d_v))  # context: [bs x q_len x n_heads * d_v]

        # Linear
        output = self.W_O(context)  # context: [bs x q_len x d_model]

        return output, attn


# Internal Cell
def get_activation_fn(activation):
    if activation == "relu":
        return nn.ReLU()
    elif activation == "gelu":
        return nn.GELU()
    else:
        return activation()


#         raise ValueError(f'{activation} is not available. You can use "relu" or "gelu"')


class _TSTEncoderLayer(Module):
    def __init__(
        self,
        q_len: int,
        d_model: int,
        n_heads: int,
        d_k: Optional[int] = None,
        d_v: Optional[int] = None,
        d_ff: int = 256,
        dropout: float = 0.1,
        activation: str = "gelu",
    ):
        assert (
            d_model // n_heads
        ), f"d_model ({d_model}) must be divisible by n_heads ({n_heads})"
        d_k = ifnone(d_k, d_model // n_heads)
        d_v = ifnone(d_v, d_model // n_heads)

        # Multi-Head attention
        self.self_attn = _MultiHeadAttention(d_model, n_heads, d_k, d_v)

        # Add & Norm
        self.dropout_attn = nn.Dropout(dropout)
        self.batchnorm_attn = nn.Sequential(
            Transpose(1, 2), nn.BatchNorm1d(d_model), Transpose(1, 2)
        )

        # Position-wise Feed-Forward
        self.ff = nn.Sequential(
            nn.Linear(d_model, d_ff),
            get_activation_fn(activation),
            nn.Dropout(dropout),
            nn.Linear(d_ff, d_model),
        )

        # Add & Norm
        self.dropout_ffn = nn.Dropout(dropout)
        self.batchnorm_ffn = nn.Sequential(
            Transpose(1, 2), nn.BatchNorm1d(d_model), Transpose(1, 2)
        )

    def forward(self, src: Tensor, mask: Optional[Tensor] = None) -> Tensor:
        # Multi-Head attention sublayer
        # Multi-Head attention
        src2, attn = self.self_attn(src, src, src, mask=mask)
        # Add & Norm
        src = src + self.dropout_attn(
            src2
        )  # Add: residual connection with residual dropout
        src = self.batchnorm_attn(src)  # Norm: batchnorm

        # Feed-forward sublayer
        # Position-wise Feed-Forward
        src2 = self.ff(src)
        # Add & Norm
        src = src + self.dropout_ffn(
            src2
        )  # Add: residual connection with residual dropout
        src = self.batchnorm_ffn(src)  # Norm: batchnorm

        return src


# Internal Cell
class _TSTEncoder(Module):
    def __init__(
        self,
        q_len,
        d_model,
        n_heads,
        d_k=None,
        d_v=None,
        d_ff=None,
        dropout=0.1,
        activation="gelu",
        n_layers=1,
    ):
        self.layers = nn.ModuleList(
            [
                _TSTEncoderLayer(
                    q_len,
                    d_model,
                    n_heads=n_heads,
                    d_k=d_k,
                    d_v=d_v,
                    d_ff=d_ff,
                    dropout=dropout,
                    activation=activation,
                )
                for i in range(n_layers)
            ]
        )

    def forward(self, src):
        output = src
        for mod in self.layers:
            output = mod(output)
        return output


# Cell
class TST(Module):
    def __init__(
        self,
        cfg,
        max_seq_len: Optional[int] = None,
        d_k: Optional[int] = None,
        d_v: Optional[int] = None,
        y_range: Optional[tuple] = None,
        **kwargs,
    ):
        r"""TST (Time Series Transformer) is a Transformer that takes continuous time series as inputs.
        As mentioned in the paper, the input must be standardized by_var based on the entire training set.
        Args:
            c_in: the number of features (aka variables, dimensions, channels) in the time series dataset.
            c_out: the number of target classes.
            seq_len: number of time steps in the time series.
            max_seq_len: useful to control the temporal resolution in long time series to avoid memory issues.
            d_model: total dimension of the model (number of features created by the model)
            n_heads:  parallel attention heads.
            d_k: size of the learned linear projection of queries and keys in the MHA. Usual values: 16-512. Default: None -> (d_model/n_heads) = 32.
            d_v: size of the learned linear projection of values in the MHA. Usual values: 16-512. Default: None -> (d_model/n_heads) = 32.
            d_ff: the dimension of the feedforward network model.
            dropout: amount of residual dropout applied in the encoder.
            act: the activation function of intermediate layer, relu or gelu.
            n_layers: the number of sub-encoder-layers in the encoder.
            fc_dropout: dropout applied to the final fully connected layer.
            y_range: range of possible y values (used in regression tasks).
            kwargs: nn.Conv1d kwargs. If not {}, a nn.Conv1d with those kwargs will be applied to original time series.
        Input shape:
            bs (batch size) x nvars (aka features, variables, dimensions, channels) x seq_len (aka time steps)
        """

        c_in = cfg["model"]["c_in"]
        c_out = cfg["model"]["c_out"]
        seq_len = cfg["model"]["seq_len"]
        max_seq_len
        n_layers = cfg["model"]["n_layers"]
        d_model = cfg["model"]["d_model"]
        n_heads = cfg["model"]["n_heads"]
        d_k
        d_v
        d_ff = cfg["model"]["d_ff"]
        dropout = cfg["model"]["dropout"]
        act = cfg["model"]["act"]
        fc_dropout = cfg["model"]["fc_dropout"]
        y_range
        verbose = cfg["model"]["verbose"]

        self.bs = cfg["model"]["batchsize"]
        self.c_out, self.seq_len = c_out, seq_len

        # Input encoding
        q_len = seq_len
        self.new_q_len = False
        if (
            max_seq_len is not None and seq_len > max_seq_len
        ):  # Control temporal resolution
            self.new_q_len = True
            q_len = max_seq_len
            tr_factor = math.ceil(seq_len / q_len)
            total_padding = tr_factor * q_len - seq_len
            padding = (total_padding // 2, total_padding - total_padding // 2)
            self.W_P = nn.Sequential(
                Pad1d(padding),
                Conv1d(
                    c_in,
                    d_model,
                    kernel_size=tr_factor,
                    padding=0,
                    stride=tr_factor),
            )
            pv(
                f"temporal resolution modified: {seq_len} --> {q_len} time steps: kernel_size={tr_factor}, stride={tr_factor}, padding={padding}.\n",
                verbose,
            )
        elif kwargs:
            self.new_q_len = True
            t = torch.rand(1, 1, seq_len)
            q_len = nn.Conv1d(1, 1, **kwargs)(t).shape[-1]
            self.W_P = nn.Conv1d(c_in, d_model, **kwargs)  # Eq 2
            pv(
                f"Conv1d with kwargs={kwargs} applied to input to create input encodings\n",
                verbose,
            )
        else:
            self.W_P = nn.Linear(
                c_in, d_model
            )  # Eq 1: projection of feature vectors onto a d-dim vector space

        # Positional encoding
        W_pos = torch.empty((q_len, d_model), device=default_device())
        nn.init.uniform_(W_pos, -0.02, 0.02)
        self.W_pos = nn.Parameter(W_pos, requires_grad=True)

        # Residual dropout
        self.dropout = nn.Dropout(dropout)

        # Encoder
        self.encoder = _TSTEncoder(
            q_len,
            d_model,
            n_heads,
            d_k=d_k,
            d_v=d_v,
            d_ff=d_ff,
            dropout=dropout,
            activation=act,
            n_layers=n_layers,
        )
        self.flatten = Flatten()

        # Head
        self.head_nf = q_len * d_model
        self.head = self.create_head(
            self.head_nf,
            c_out * seq_len,
            act=act,
            fc_dropout=fc_dropout,
            y_range=y_range,
        )

    def create_head(
        self, nf, c_out, act="gelu", fc_dropout=0.0, y_range=None, **kwargs
    ):
        layers = [get_activation_fn(act), Flatten()]
        if fc_dropout:
            layers += [nn.Dropout(fc_dropout)]
        layers += [nn.Linear(nf, c_out)]
        if y_range:
            layers += [SigmoidRange(*y_range)]
        return nn.Sequential(*layers)

    def forward(
        self, x: Tensor, mask: Optional[Tensor] = None
    ) -> Tensor:  # x: [bs x nvars x q_len]
        # Input encoding
        if self.new_q_len:
            # Eq 2        # u: [bs x d_model x q_len] transposed to [bs x q_len
            # x d_model]
            u = self.W_P(x).transpose(2, 1)
        else:
            # Eq 1                     # u: [bs x q_len x nvars] converted to
            # [bs x q_len x d_model]
            u = self.W_P(x.transpose(2, 1))

        # Positional encoding
        u = self.dropout(u + self.W_pos)

        # Encoder
        z = self.encoder(u)  # z: [bs x q_len x d_model]
        z = z.transpose(2, 1).contiguous()  # z: [bs x d_model x q_len]
        z = self.head(z)
        z = z.reshape(self.bs, self.c_out, self.seq_len)

        # Classification/ Regression head
        return z  # output: [bs x c_out]
