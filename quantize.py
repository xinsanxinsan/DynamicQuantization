#!/home/sunhanbo/software/anaconda3/bin/python
#-*-coding:utf-8-*-
import torch
import torch.nn as nn
from torch.autograd import Function
import torch.nn.functional as F

# power scale record
power_scale = 0
power_weight_scale = 0
power_activation_scale = 0
# 在本次的实验假设中，对输入定点，对权重定点，对输出定点
# 但是不要求前一层的输出一定是下一层的输入
# 不需要保留定点的系数，保留相关结果即可
# 本工程对权重的定点不做严格要求，期望能够通过一种定点训练方法找到动态的量化范围
# quantize Function
class QuantizeFunction(Function):
    @staticmethod
    def forward(ctx, input, fix_config, training, last_value = None):
        # 对于不同的模式，采用完全不同的量化方法
        global power_scale
        global power_weight_scale
        global power_activation_scale
        if fix_config['mode'] == 'input':
            # 此部分只对整个网络的输入做变换，数据范围是[0,1]
            power_scale = 1
            power_activation_scale = 1
            return input
        elif fix_config['mode'] == 'activation_in':
            # 此部分对输入的激活做变换，默认输入的激活均为非负数，这样可以忽略负数的影响
            if training:
                momentum = fix_config['momentum']
                last_value.data[0] = momentum * last_value.item() + (1 - momentum) * torch.max(torch.abs(input)).item()
            scale = last_value.item()
            thres = 2 ** (fix_config['qbit'] - 1) - 1
            output = torch.div(input, scale)
            power_scale = scale**2
            power_activation_scale = scale
            assert torch.min(output).item() >= 0
            return output.clamp_(-1, 1).mul_(thres).round_().div(thres/scale)
        elif fix_config['mode'] == 'weight':
            # 此部分对权重做变换，直接采用最大值，可以尽可能少地产生误差，网络权重有正有负
            scale = torch.max(torch.abs(input)).item()
            # scale = 3*torch.std(input).item() + torch.abs(torch.mean(input)).item()
            thres = 2 ** (fix_config['qbit'] - 1) - 1
            output = torch.div(input, scale)
            power_scale *= scale
            power_weight_scale = scale
            return output.clamp_(-1, 1).mul_(thres).round_().div(thres/scale)
        elif fix_config['mode'] == 'activation_out':
            # 此部分对输出的激活做变换，输出的激活有正有负，可能需要采用非均匀量化的方式
            # 这部分采用3sigma原则来对范围进行限制
            # 不需要对power_scale进行处理
            if training:
                momentum = fix_config['momentum']
                last_value.data[0] = momentum * last_value.item() + (1 - momentum) * (3*torch.std(input).item() + torch.abs(torch.mean(input)).item())
            scale = last_value.item()
            thres = 2 ** (fix_config['qbit'] - 1) - 1
            output = torch.div(input, scale)
            return output.clamp_(-1, 1).mul_(thres).round_().div(thres/scale)
            # ratio = 3.8
            # thres = 2 ** (fix_config['qbit'])
            # output = torch.div(input, scale)
            # output = output.mul_(ratio).sigmoid_().mul_(thres).round_().clamp_(1, thres - 1).div_(thres).reciprocal_().sub_(1).log_().div(-ratio/scale)
            # return output
        else:
            raise NotImplementedError
    @staticmethod
    def backward(ctx, grad_output):
        return grad_output, None, None, None, None
Quantize = QuantizeFunction.apply

# 量化的卷积层，不统计每层的crossbar能耗
class QuantizeConv2d(nn.Module):
    def __init__(self, fix_config_dict, in_channels, out_channels, kernel_size, \
                 stride = 1, padding = 0, dilation = 1, groups = 1, bias = True):
        super(QuantizeConv2d, self).__init__()
        self.conv2d = nn.Conv2d(in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias)
        self.last_value_input = nn.Parameter(torch.ones((1)))
        self.last_value_output = nn.Parameter(torch.ones((1)))
        self.input_fix_config = fix_config_dict['input']
        self.weight_fix_config = fix_config_dict['weight']
        self.output_fix_config = fix_config_dict['output']
    def forward(self, x):
        # dynamic quantize
        quantize_input = Quantize(x,
                                  self.input_fix_config,
                                  self.training,
                                  self.last_value_input,
                                  )
        quantize_weight = Quantize(self.conv2d.weight,
                                   self.weight_fix_config,
                                   self.training,
                                   None,
                                   )
        output = F.conv2d(quantize_input,
                          quantize_weight,
                          self.conv2d.bias,
                          self.conv2d.stride,
                          self.conv2d.padding,
                          self.conv2d.dilation,
                          self.conv2d.groups,
                          )
        quantize_output = Quantize(output,
                                   self.output_fix_config,
                                   self.training,
                                   self.last_value_output,
                                   )
        return quantize_output
    def extra_repr(self):
        extra_def = []
        for name, config in zip(['input', 'weight', 'output'], [self.input_fix_config, self.weight_fix_config, self.output_fix_config]):
            part_def = name
            for key, value in config.items():
                part_def = part_def + ' ' + key + ':' + str(value) + ','
            extra_def.append(part_def)
        return '\n'.join(extra_def)

# 量化的卷积层，统计每层的crossbar能耗
class QuantizePowerConv2d(nn.Module):
    def __init__(self, fix_config_dict, in_channels, out_channels, kernel_size, \
                 stride = 1, padding = 0, dilation = 1, groups = 1, bias = True):
        super(QuantizePowerConv2d, self).__init__()
        self.conv2d = nn.Conv2d(in_channels, out_channels, kernel_size, stride, padding, dilation, groups, bias)
        self.last_value_input = nn.Parameter(torch.zeros((1)))
        self.last_value_output = nn.Parameter(torch.zeros((1)))
        self.input_fix_config = fix_config_dict['input']
        self.weight_fix_config = fix_config_dict['weight']
        self.output_fix_config = fix_config_dict['output']
    def forward(self, x, power):
        # dynamic quantize
        quantize_input = Quantize(x,
                                  self.input_fix_config,
                                  self.training,
                                  self.last_value_input,
                                  )
        quantize_weight = Quantize(self.conv2d.weight,
                                   self.weight_fix_config,
                                   self.training,
                                   None,
                                   )
        output = F.conv2d(quantize_input,
                          quantize_weight,
                          self.conv2d.bias,
                          self.conv2d.stride,
                          self.conv2d.padding,
                          self.conv2d.dilation,
                          self.conv2d.groups,
                          )
        quantize_output = Quantize(output,
                                   self.output_fix_config,
                                   self.training,
                                   self.last_value_output,
                                   )
        # power
        # if self.training:
        #     norm_x = quantize_input / torch.mean(torch.abs(quantize_input))
        #     norm_w = quantize_weight / torch.mean(torch.abs(quantize_weight))
        #     # norm_x = x / torch.mean(torch.abs(x))
        #     # norm_w = self.conv2d.weight / torch.mean(torch.abs(self.conv2d.weight))
        #     square_sum = F.conv2d(torch.mul(norm_x, norm_x),
        #                           torch.abs(norm_w),
        #                           None,
        #                           self.conv2d.stride,
        #                           self.conv2d.padding,
        #                           self.conv2d.dilation,
        #                           self.conv2d.groups,
        #                           )
        if self.training:
            square_sum = F.conv2d(torch.mul(quantize_input, quantize_input),
                                  torch.abs(quantize_weight),
                                  None,
                                  self.conv2d.stride,
                                  self.conv2d.padding,
                                  self.conv2d.dilation,
                                  self.conv2d.groups,
                                  )
            square_sum = square_sum / power_scale
            power.add_(torch.sum(square_sum))
        else:
            # RR = self.conv2d.kernel_size[0] * self.conv2d.kernel_size[1] * self.conv2d.in_channels * 0.005
            if self.input_fix_config['mode'] == 'input':
                quantize_input.div_(power_activation_scale/(2**8-1))
                activation_bit = 8
            else:
                quantize_input.div_(power_activation_scale/(2**self.input_fix_config['qbit']-1))
                activation_bit = self.input_fix_config['qbit']
            quantize_weight.div_(power_weight_scale/(2**(self.weight_fix_config['qbit']-1)-1)).abs_()
            weight_bit = self.weight_fix_config['qbit'] - 1
            for i in range(activation_bit):
                tmp_quantize_input = torch.fmod(quantize_input, 2).round()
                quantize_input.div_(2).floor_()
                record_quantize_weight = quantize_weight.clone()
                for j in range(weight_bit):
                    tmp_quantize_weight = torch.fmod(record_quantize_weight, 2).round()*4+1
                    record_quantize_weight.div_(2).floor_()
                    square_sum = F.conv2d(tmp_quantize_input,
                                          tmp_quantize_weight,
                                          None,
                                          self.conv2d.stride,
                                          self.conv2d.padding,
                                          self.conv2d.dilation,
                                          self.conv2d.groups,
                                          )
                    RR = torch.std(square_sum).item() / 10
                    power.add_(torch.sum(square_sum * (RR+1)))
        return quantize_output
    def extra_repr(self):
        extra_def = []
        for name, config in zip(['input', 'weight', 'output'], [self.input_fix_config, self.weight_fix_config, self.output_fix_config]):
            part_def = name
            for key, value in config.items():
                part_def = part_def + ' ' + key + ':' + str(value) + ','
            extra_def.append(part_def)
