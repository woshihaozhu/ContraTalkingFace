import torch
import torch.nn as nn
from .shufflenetv2 import ShuffleNetV2
from .tcn import MultibranchTemporalConvNet, TemporalConvNet
from einops import rearrange, repeat
from einops.layers.torch import Rearrange
import json, os


def load_json( json_fp ):
    with open( json_fp, 'r' ) as f:
        json_content = json.load(f)
    return json_content

# -- auxiliary functions
def threeD_to_2D_tensor(x):
    n_batch, n_channels, s_time, sx, sy = x.shape
    x = x.transpose(1, 2)
    return x.reshape(n_batch*s_time, n_channels, sx, sy)


def _average_batch(x, lengths, B):
    return torch.stack([torch.mean(x[index][:, 0:i], 1) for index, i in enumerate(lengths)], 0)


class MultiscaleMultibranchTCN(nn.Module):
    def __init__(self, input_size, num_channels, num_classes, tcn_options, dropout, relu_type, dwpw=False):
        super(MultiscaleMultibranchTCN, self).__init__()

        self.kernel_sizes = tcn_options['kernel_size']
        self.num_kernels = len(self.kernel_sizes)

        self.mb_ms_tcn = MultibranchTemporalConvNet(
            input_size, num_channels, tcn_options, dropout=dropout, relu_type=relu_type, dwpw=dwpw)
        self.tcn_output = nn.Linear(num_channels[-1], num_classes)

        self.consensus_func = _average_batch

    def forward(self, x, lengths, B, extract_feats=False):
        # x needs to have dimension (N, C, L) in order to be passed into CNN
        xtrans = x.transpose(1, 2)
        if extract_feats:
            out = self.mb_ms_tcn(xtrans)
            return out.unsqueeze(-2)
        else:
            return xtrans.unsqueeze(-2)
            #out = self.consensus_func( out, lengths, B )
            #return self.tcn_output(out)
            #return out.view(out.size(0), -1, 1, 1)


class TCN(nn.Module):
    """Implements Temporal Convolutional Network (TCN)
    __https://arxiv.org/pdf/1803.01271.pdf
    """

    def __init__(self, input_size, num_channels, num_classes, tcn_options, dropout, relu_type, dwpw=False):
        super(TCN, self).__init__()
        self.tcn_trunk = TemporalConvNet(
            input_size, num_channels, dropout=dropout, tcn_options=tcn_options, relu_type=relu_type, dwpw=dwpw)
        self.tcn_output = nn.Linear(num_channels[-1], num_classes)

        # average tensor along final axis
        self.consensus_func = _average_batch

        self.has_aux_losses = False

    def forward(self, x, lengths, B, extract_feats=False):
        # x needs to have dimension (N, C, L) in order to be passed into CNN
        if extract_feats:
            #TODO add auxilliary module below
            x = self.tcn_trunk(x.transpose(1, 2))  # (bs, 512, length)
            out = self.consensus_func(x, lengths, B) # using module only in test mode (bs, 512)
            return x.unsqueeze(-2), out
        else:
            return x.unsqueeze(-2)
        #    x = self.consensus_func(x, lengths, B)
        #    return self.tcn_output(x)


class Lipreading(nn.Module):
    def __init__(self, hidden_dim=256, num_classes=500, backbone_type='shufflenet',
                 relu_type='prelu', tcn_options={}, width_mult=1.0, extract_feats=False):
        super(Lipreading, self).__init__()
        self.extract_feats = extract_feats
        self.backbone_type = backbone_type

        assert width_mult in [0.5, 1.0, 1.5, 2.0], "Width multiplier not correct"
        shufflenet = ShuffleNetV2(input_size=96, width_mult=width_mult)
        self.trunk = nn.Sequential(shufflenet.features, shufflenet.conv_last, shufflenet.globalpool)
        self.frontend_nout = 24
        self.backend_out = 1024 if width_mult != 2.0 else 2048
        self.stage_out_channels = shufflenet.stage_out_channels[-1]

        frontend_relu = nn.PReLU(num_parameters=self.frontend_nout) if relu_type == 'prelu' else nn.ReLU()
        self.frontend3D = nn.Sequential(
            nn.Conv3d(1, self.frontend_nout, kernel_size=(5, 7, 7),
                      stride=(1, 2, 2), padding=(2, 3, 3), bias=False),
            nn.BatchNorm3d(self.frontend_nout),
            frontend_relu,
            nn.MaxPool3d(kernel_size=(1, 3, 3), stride=(1, 2, 2), padding=(0, 1, 1)))
        tcn_class = TCN if len(tcn_options['kernel_size']) == 1 else MultiscaleMultibranchTCN
        self.tcn = tcn_class(input_size=self.backend_out,
                             num_channels=[hidden_dim*len(tcn_options['kernel_size'])*tcn_options['width_mult']]*tcn_options['num_layers'],
                             num_classes=num_classes,
                             tcn_options=tcn_options,
                             dropout=tcn_options['dropout'],
                             relu_type=relu_type,
                             dwpw=tcn_options['dwpw'],)

    def forward(self, x, lengths):
        B, C, T, H, W = x.size()
        if (type(lengths) == int):
            lengths = [lengths] * B
        x = self.frontend3D(x) # output should be B x C2 x Tnew x Hnew x Wnew, such as (4, 24, 64, 22, 22)
        Tnew = x.shape[2]    
        x = threeD_to_2D_tensor(x) # x.size() is (BxT, C2, Hnew, Wnew), such as (256, 24, 22, 22)
        x = self.trunk(x)  # x.size() is (B*T, 1024, 1, 1)
        if self.backbone_type == 'shufflenet':
            x = x.view(-1, self.stage_out_channels) # (256, 1024)
        x = x.view(B, Tnew, x.size(1))  # (4, 64, 1024)     change to x.view(B, Tnew, Hnew, Wnew) (4, 64, 32, 32)
        #return x if self.extract_feats else self.tcn(x, lengths, B)
        return self.tcn(x, lengths, B, self.extract_feats) #(B, 512, 1, T)

def build_lipreadingnet(config_path, weights='', extract_feats=False):
    if os.path.exists(config_path):
        args_loaded = load_json(config_path)
        print('Lipreading configuration file loaded.')
        tcn_options = { 'num_layers': args_loaded['tcn_num_layers'],
                        'kernel_size': args_loaded['tcn_kernel_size'],
                        'dropout': args_loaded['tcn_dropout'],
                        'dwpw': args_loaded['tcn_dwpw'],
                        'width_mult': args_loaded['tcn_width_mult']}                 
    net = Lipreading(tcn_options=tcn_options,
                    backbone_type=args_loaded['backbone_type'],
                    relu_type=args_loaded['relu_type'],
                    width_mult=args_loaded['width_mult'],
                    extract_feats=extract_feats)

    if len(weights) > 0:
        print('Loading weights for lipreading stream')
        net.load_state_dict(torch.load(weights, map_location='cuda:0'))
    return net