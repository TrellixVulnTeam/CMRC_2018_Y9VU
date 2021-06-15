# coding=utf-8
import math
import torch
from torch.optim.optimizer import Optimizer
from torch.nn.utils import clip_grad_norm_


def warmup_cosine(x, warmup=0.002):
    if x < warmup:
        return x / warmup
    return 0.5 * (1.0 + torch.cos(math.pi * x))


def warmup_constant(x, warmup=0.002):
    if x < warmup:
        return x / warmup
    return 1.0


def warmup_linear(x, warmup=0.002):
    if x < warmup:
        return x / warmup
    return (1.0 - x) / (1.0 - warmup)


def warmup_fix(step, warmup_step):
    return min(1.0, step / warmup_step)


SCHEDULES = {
    'warmup_cosine': warmup_cosine,
    'warmup_constant': warmup_constant,
    'warmup_linear': warmup_linear,
    'warmup_fix': warmup_fix
}


class BERTAdam(Optimizer):

    def __init__(self, params, lr, warmup=-1, t_total=-1, schedule='warmup_linear',
                 b1=0.9, b2=0.999, e=1e-6, weight_decay_rate=0.01, cycle_step=None,
                 max_grad_norm=1.0):
        if lr is not None and not lr >= 0.0:
            raise ValueError("Invalid learning rate: {} - should be >= 0.0".format(lr))
        if schedule not in SCHEDULES:
            raise ValueError("Invalid schedule parameter: {}".format(schedule))
        if not 0.0 <= warmup < 1.0 and not warmup == -1:
            raise ValueError("Invalid warmup: {} - should be in [0.0, 1.0[ or -1".format(warmup))
        if not 0.0 <= b1 < 1.0:
            raise ValueError("Invalid b1 parameter: {} - should be in [0.0, 1.0[".format(b1))
        if not 0.0 <= b2 < 1.0:
            raise ValueError("Invalid b2 parameter: {} - should be in [0.0, 1.0[".format(b2))
        if not e >= 0.0:
            raise ValueError("Invalid epsilon value: {} - should be >= 0.0".format(e))
        defaults = dict(lr=lr, schedule=schedule, warmup=warmup, t_total=t_total,
                        b1=b1, b2=b2, e=e, weight_decay_rate=weight_decay_rate,
                        max_grad_norm=max_grad_norm, cycle_step=cycle_step)
        super(BERTAdam, self).__init__(params, defaults)

    def step(self, closure=None):
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data
                if grad.is_sparse:
                    raise RuntimeError('Adam does not support sparse gradients, please consider SparseAdam instead')

                state = self.state[p]

                if len(state) == 0:
                    state['step'] = 0
                    # Exponential moving average of gradient values
                    state['next_m'] = torch.zeros_like(p.data)
                    # Exponential moving average of squared gradient values
                    state['next_v'] = torch.zeros_like(p.data)

                next_m, next_v = state['next_m'], state['next_v']
                beta1, beta2 = group['b1'], group['b2']

                if group['max_grad_norm'] > 0:
                    clip_grad_norm_(p, group['max_grad_norm'])

                next_m.mul_(beta1).add_(1 - beta1, grad)
                next_v.mul_(beta2).addcmul_(1 - beta2, grad, grad)
                update = next_m / (next_v.sqrt() + group['e'])

                if group['weight_decay_rate'] > 0.0:
                    update += group['weight_decay_rate'] * p.data

                schedule_fct = SCHEDULES[group['schedule']]
                if group['cycle_step'] is not None and state['step'] > group['cycle_step']:
                    lr_scheduled = group['lr'] * (1 - ((state['step'] % group['cycle_step']) / group['cycle_step']))
                elif group['t_total'] != -1 and group['schedule'] != 'warmup_fix':
                    lr_scheduled = group['lr'] * schedule_fct(state['step'] / group['t_total'], group['warmup'])
                elif group['schedule'] == 'warmup_fix':
                    lr_scheduled = group['lr'] * schedule_fct(state['step'], group['warmup'] * group['t_total'])
                else:
                    lr_scheduled = group['lr']

                update_with_lr = lr_scheduled * update
                p.data.add_(-update_with_lr)

                state['step'] += 1
        return loss


def get_optimization(model,
                     float16,
                     learning_rate,
                     total_steps,
                     schedule,
                     warmup_rate,
                     weight_decay_rate,
                     max_grad_norm,
                     opt_pooler=False):
    assert 0.0 <= warmup_rate <= 1.0
    param_optimizer = list(model.named_parameters())

    if opt_pooler is False:
        param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_parameters = [
        {'params': [p for n, p in param_optimizer if not any([nd in n for nd in no_decay])],
         'weight_decay_rate': weight_decay_rate},
        {'params': [p for n, p in param_optimizer if any([nd in n for nd in no_decay])],
         'weight_decay_rate': 0.0}
    ]
    if float16:
        from apex.contrib.optimizers import FP16_Optimizer
        from apex.contrib.optimizers import FusedAdam

        optimizer = FusedAdam(optimizer_parameters,
                              lr=learning_rate,
                              bias_correction=False,
                              max_grad_norm=max_grad_norm)
        optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
    else:
        optimizer = BERTAdam(params=optimizer_parameters,
                             lr=learning_rate,
                             warmup=warmup_rate,
                             max_grad_norm=max_grad_norm,
                             t_total=total_steps,
                             schedule=schedule,
                             weight_decay_rate=weight_decay_rate)
    return optimizer
