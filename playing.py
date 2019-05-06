import logging

logger = logging.getLogger('logger')

import json
from datetime import datetime
import argparse

import torch
import torchvision
import os
import torchvision.transforms as transforms
from collections import defaultdict, OrderedDict
from tensorboardX import SummaryWriter
import torchvision.models as models

from helper import Helper
from image_helper import ImageHelper
from models.densenet import DenseNet
from models.simple import Net, FlexiNet
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tqdm import tqdm as tqdm
import time
import random
import yaml

from models.resnet import Res, PretrainedRes
from utils.utils import dict_html, create_table, plot_confusion_matrix
from inception import *

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

layout = {'accuracy_per_class': {
    'accuracy_per_class': ['Multiline', ['accuracy_per_class/accuracy_var',
                                         'accuracy_per_class/accuracy_min',
                                         'accuracy_per_class/accuracy_max',
                                         'accuracy_per_class/unbalanced']]}}


def plot(x, y, name):
    writer.add_scalar(tag=name, scalar_value=y, global_step=x)


def compute_norm(model, norm_type=2):
    total_norm = 0
    for p in model.parameters():
        param_norm = p.grad.data.norm(norm_type)
        total_norm += param_norm.item() ** norm_type
    total_norm = total_norm ** (1. / norm_type)
    return total_norm



def test(net, epoch, name, testloader, vis=True):
    net.eval()
    correct = 0
    total = 0
    i=0
    correct_labels = []
    predict_labels = []
    with torch.no_grad():
        for data in tqdm(testloader):
            if helper.params['dataset'] == 'dif':
                inputs, idxs, labels = data
            else:
                inputs, labels = data
            inputs = inputs.to(device)
            labels = labels.to(device)
            outputs = net(inputs)
            _, predicted = torch.max(outputs.data, 1)
            predict_labels.extend([x.item() for x in predicted])
            correct_labels.extend([x.item() for x in labels])
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    logger.info(f'Name: {name}. Epoch {epoch}. acc: {100 * correct / total}')
    if vis:
        plot(epoch, 100 * correct / total, name)
        fig, cm = plot_confusion_matrix(correct_labels, predict_labels, labels=helper.labels, normalize=True)
        acc_list = list()

        for i, name in enumerate(helper.labels):
            class_acc = cm[i][i]/cm[i].sum()
            plot(epoch, class_acc, name=f'accuracy_per_class/class_{name}')
            acc_list.append(class_acc)
        plot(epoch, np.var(acc_list), name='accuracy_per_class/accuracy_var')
        plot(epoch, np.max(acc_list), name='accuracy_per_class/accuracy_max')
        plot(epoch, np.min(acc_list), name='accuracy_per_class/accuracy_min')
        cm_name = f'{helper.params["folder_path"]}/cm_{epoch}.pt'
        writer.add_figure(figure=fig, global_step=epoch, tag='tag/normalized')
        fig, cm = plot_confusion_matrix(correct_labels, predict_labels, labels=helper.labels, normalize=False)
        torch.save(cm, cm_name)
        writer.add_figure(figure=fig, global_step=epoch, tag='tag/unnormalized')
    return 100 * correct / total


def train_dp(trainloader, model, optimizer, epoch):
    norm_type = 2
    model.train()
    running_loss = 0.0
    label_norms = defaultdict(list)
    for i, data in tqdm(enumerate(trainloader, 0), leave=True):
        if helper.params['dataset'] == 'dif':
            inputs, idxs, labels = data
        else:
            inputs, labels = data
        # print('labels: ', labels)
        inputs = inputs.to(device)
        labels = labels.to(device)
        optimizer.zero_grad()

        outputs = model(inputs)
        loss = criterion(outputs, labels)
        running_loss += torch.mean(loss).item()

        losses = torch.mean(loss.reshape(num_microbatches, -1), dim=1)
        saved_var = dict()
        for tensor_name, tensor in model.named_parameters():
            saved_var[tensor_name] = torch.zeros_like(tensor)

        for pos, j in enumerate(losses):
            j.backward(retain_graph=True)
            # if False:
            #     total_norm = helper.clip_grad_scale_by_layer_norm(model.parameters(), S)
            # else:
            total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), S)
            if helper.params['dataset'] == 'dif':
                label_norms[f'{labels[pos]}_{helper.label_skin_list[idxs[pos]]}'].append(total_norm)
            else:
                label_norms[int(labels[pos])].append(total_norm)

            for tensor_name, tensor in model.named_parameters():
                  if tensor.grad is not None:
                     new_grad = tensor.grad
                #print('new grad: ', new_grad)
                     saved_var[tensor_name].add_(new_grad)
            model.zero_grad()

        for tensor_name, tensor in model.named_parameters():
            if tensor.grad is not None:
                if device.type == 'cuda':
                    saved_var[tensor_name].add_(torch.cuda.FloatTensor(tensor.grad.shape).normal_(0, sigma))
                else:
                    saved_var[tensor_name].add_(torch.FloatTensor(tensor.grad.shape).normal_(0, sigma))
                tensor.grad = saved_var[tensor_name] / num_microbatches
        optimizer.step()

        if i > 0 and i % 20 == 0:
            #             logger.info('[%d, %5d] loss: %.3f' %
            #                   (epoch + 1, i + 1, running_loss / 2000))
            plot(epoch * len(trainloader) + i, running_loss, 'Train Loss')
            running_loss = 0.0

    for pos, norms in sorted(label_norms.items(), key=lambda x: x[0]):
        print(f"{pos}: {np.mean(norms)}")
        if helper.params['dataset'] == 'dif':
            plot(epoch, np.mean(norms), f'dif_norms_class/{pos}')
        else:
            plot(epoch, np.mean(norms), f'norms/class_{pos}')


def train(trainloader, model, optimizer, epoch):
    model.train()
    running_loss = 0.0
    for i, data in tqdm(enumerate(trainloader, 0), leave=True):
        # get the inputs
        if helper.params['dataset'] == 'dif':
            inputs, idxs, labels = data
        else:
            inputs, labels = data
        inputs = inputs.to(device)
        labels = labels.to(device)
        # zero the parameter gradients
        optimizer.zero_grad()

        # forward + backward + optimize
        outputs = model(inputs)
        loss = criterion(outputs, labels)

        loss.backward()
        optimizer.step()
        # print statistics
        running_loss += loss.item()
        if i > 0 and i % 20 == 0:
            #             logger.info('[%d, %5d] loss: %.3f' %
            #                   (epoch + 1, i + 1, running_loss / 2000))
            plot(epoch * len(trainloader) + i, running_loss, 'Train Loss')
            running_loss = 0.0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PPDL')
    parser.add_argument('--params', dest='params', default='utils/params.yaml')
    parser.add_argument('--name', dest='name', required=True)

    args = parser.parse_args()
    d = datetime.now().strftime('%b.%d_%H.%M.%S')
    writer = SummaryWriter(log_dir=f'runs/{args.name}')
    writer.add_custom_scalars(layout)

    with open(args.params) as f:
        params = yaml.load(f)
    helper = ImageHelper(current_time=d, params=params, name='utk')
    logger.addHandler(logging.FileHandler(filename=f'{helper.folder_path}/log.txt'))
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.DEBUG)
    logger.info(f'current path: {helper.folder_path}')
    batch_size = int(helper.params['batch_size'])
    num_microbatches = int(helper.params['num_microbatches'])
    lr = float(helper.params['lr'])
    momentum = float(helper.params['momentum'])
    decay = float(helper.params['decay'])
    epochs = int(helper.params['epochs'])
    S = float(helper.params['S'])
    z = float(helper.params['z'])
    sigma = z * S
    dp = helper.params['dp']
    mu = helper.params['mu']
    logger.info(f'DP: {dp}')

    logger.info(batch_size)
    logger.info(lr)
    logger.info(momentum)
    if helper.params['dataset'] == 'inat':
        helper.load_inat_data()
        helper.balance_loaders()
    elif helper.params['dataset'] == 'dif':
        helper.load_dif_data()
        helper.get_unbalanced_faces()
    else:
        helper.load_cifar_data(dataset=params['dataset'])
        logger.info('before loader')
        helper.create_loaders()
        logger.info('after loader')
        helper.sampler_per_class()
        logger.info('after sampler')
        helper.sampler_exponential_class(mu=mu, total_number=params['ds_size'], key_to_drop=params['key_to_drop'],
                                        number_of_entries=params['number_of_entries'])
        logger.info('after sampler expo')
        helper.sampler_exponential_class_test(mu=mu, key_to_drop=params['key_to_drop'],
              number_of_entries_test=params['number_of_entries_test'])
        logger.info('after sampler test')

    helper.compute_rdp()
    if helper.params['dataset'] == 'cifar10':
        num_classes = 10
    elif helper.params['dataset'] == 'cifar100':
        num_classes = 100
    elif helper.params['dataset'] == 'inat':
        num_classes = 14
    elif helper.params['dataset'] == 'dif':
        num_classes = len(helper.labels)
    else:
        num_classes = 10

    if helper.params['model'] == 'densenet':
        net = DenseNet(num_classes=num_classes, depth=helper.params['densenet_depth'])
    elif helper.params['model'] == 'resnet':
        logger.info(f'Model size: {num_classes}')
        net = models.resnet18(num_classes=num_classes)
    elif helper.params['model'] == 'PretrainedRes':
        net = PretrainedRes(num_classes)
    elif helper.params['model'] == 'FlexiNet':
        net = FlexiNet(3, num_classes)
    elif helper.params['model'] == 'inception':
        net = inception_v3(pretrained=True)
        net.fc = nn.Linear(2048, num_classes)
        net.aux_logits = False
        #model = torch.nn.DataParallel(model).cuda()
        net = net.cuda()
    else:
        net = Net()

    net.to(device)
    logger.info(f'Total number of params for model {helper.params["model"]}: {sum(p.numel() for p in net.parameters() if p.requires_grad)}')
    if dp:
        criterion = nn.CrossEntropyLoss(reduction='none')
    else:
        criterion = nn.CrossEntropyLoss()

    if helper.params['optimizer'] == 'SGD':
        optimizer = optim.SGD(net.parameters(), lr=lr, momentum=momentum, weight_decay=decay)
    elif helper.params['optimizer'] == 'Adam':
        optimizer = optim.Adam(net.parameters(), lr=lr, weight_decay=decay)
    else:
        raise Exception('Specify `optimizer` in params.yaml.')


    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                     milestones=[0.5 * epochs,
                                                                 0.75 * epochs], gamma=0.1)

    table = create_table(helper.params)
    writer.add_text('Model Params', table)
    logger.info(helper.labels)

    for epoch in range(1, epochs):  # loop over the dataset multiple times
        if dp:
            train_dp(helper.train_loader, net, optimizer, epoch)
        else:
            train(helper.train_loader, net, optimizer, epoch)
        if helper.params['scheduler']:
            scheduler.step()
        acc = test(net, epoch, "accuracy", helper.test_loader, vis=True)
        if helper.params['dataset'] == 'dif':
            for name, value in sorted(helper.unbalanced_loaders.items(), key=lambda x: x[0]):
                unb_acc = test(net, epoch, name, value, vis=False)
                if helper.params['dataset'] == 'dif':
                    plot(epoch, unb_acc, name=f'dif_unbalanced/{name}')
                else:
                    plot(epoch, unb_acc, name=f'accuracy_unbalanced/{name}')


        helper.save_model(net, epoch, acc)
    logger.info(f"Finished training for model: {helper.current_time}")
