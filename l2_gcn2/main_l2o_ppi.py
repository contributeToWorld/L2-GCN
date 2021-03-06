import torch
import torch.nn as nn
from torch.nn import init
from torch.autograd import Variable

import numpy as np
import time
import random
from sklearn.metrics import f1_score
import os
import yaml
import argparse
import scipy.sparse as sps
import random

import l2_gcn2.utils as utils
import l2_gcn2.net as net


def run_l2o(dataset_load_func, seed, dataset, config_file, layer_num, epoch_num, controller_len):

    ##################################
    ###### learn to stop

    # dataset load
    feat_data, labels, Adj_hat, dataset_split = dataset_load_func()
    print('Finished loading dataset.')

    # config parameter load
    with open('./config/' + config_file) as f:
        args = yaml.load(f)
    if not layer_num is None:
        args['layer_num'] = layer_num
    if not epoch_num is None:
        args['layer_train_batch'] = epoch_num
    if not controller_len is None:
        args['controller_len'] = controller_len

    num_nodes = args['node_num']

    # train, val and test set index
    train = dataset_split['train']
    train.sort()
    val = dataset_split['val']
    test = dataset_split['test']
    test.sort()
    print('trainset size', len(train),
          'testset size', len(test))

    # feature and label generate
    feat_train = torch.FloatTensor(feat_data)[train, :]
    label_train = labels[train]
    feat_test = torch.FloatTensor(feat_data)

    # Adj matrix generate
    Adj = Adj_hat
    Adj_eye = sps.eye(num_nodes, dtype=np.float32).tocsr()

    Adj_train = Adj[train, :][:, train]
    D_train = Adj_train.sum(axis=0)
    Adj_train = Adj_train.multiply(1/D_train.transpose())
    Adj_train = Adj_train + Adj_eye[train, :][:, train]

    Adj_test = Adj
    D_test = Adj_test.sum(axis=0)
    Adj_test = Adj_test.multiply(1/D_test.transpose())
    Adj_test = Adj_test + Adj_eye

    print('Finished generating adj matrix.')


    # layerwise training

    loss_func = nn.BCELoss()
    sigmoid = nn.Sigmoid()
    relu = nn.ReLU(inplace=True)

    ###### learn to stop

    controller_l2o = net.controller_l2o(args['layer_num'], args['controller_len']).to(torch.device(args['device']))
    optimizer_l2o = torch.optim.Adam(controller_l2o.parameters(), lr=args['l2o_learning_rate'])
    baseline = args['baseline_reward']

    ######

    feeder_train_sample = utils.feeder_sample(feat_data, labels, Adj, Adj_eye, train, args['total_round'], args['sample_node_num'])
    dataset_train_sample = torch.utils.data.DataLoader(dataset=feeder_train_sample, batch_size=1)

    predefine_action = np.zeros((5 * args['init_round'], args['controller_len']), dtype=np.int32)
    # assert args['controller_len'] % 8 == 0
    predefine_action[:, args['controller_len']-1] = 1
    if args['layer_num'] == 2:
        for ir in range(args['init_round']):
            for n in range(5):
                predefine_action[ir*5, int(args['controller_len']/4)] = 1
                predefine_action[ir*5+1, int(args['controller_len']/8*3)] = 1
                predefine_action[ir*5+2, int(args['controller_len']/2)] = 1
                predefine_action[ir*5+3, int(args['controller_len']/8*5)] = 1
                predefine_action[ir*5+4, int(args['controller_len']/4*3)] = 1
    else:
        for n in range(args['layer_num'] - 2):
            predefine_action[:, int(args['controller_len']/args['layer_num']*(n+1)-1)] = 1
        for ir in range(args['init_round']):
            for n in range(5):
                predefine_action[ir*5, int(args['controller_len']/args['layer_num']*(args['layer_num']-2)+args['controller_len']/args['layer_num']*2/4)] = 1
                predefine_action[ir*5+1, int(args['controller_len']/args['layer_num']*(args['layer_num']-2)+args['controller_len']/args['layer_num']*2/8*3)] = 1
                predefine_action[ir*5+2, int(args['controller_len']/args['layer_num']*(args['layer_num']-2)+args['controller_len']/args['layer_num']*2/2)] = 1
                predefine_action[ir*5+3, int(args['controller_len']/args['layer_num']*(args['layer_num']-2)+args['controller_len']/args['layer_num']*2/8*5)] = 1
                predefine_action[ir*5+4, int(args['controller_len']/args['layer_num']*(args['layer_num']-2)+args['controller_len']/args['layer_num']*2/4*3)] = 1

    net_test = net.net_test()
    for feat_train, label_train, train_sample, iround in dataset_train_sample:

        feat_train = feat_train.view(args['sample_node_num'], args['feat_dim'])
        label_train = label_train.view(1024, 121)

        Adj_train = Adj_hat[train_sample, :][:, train_sample]
        D = Adj_train.sum(axis=0)
        Adj_train = Adj_train.multiply(1/D.transpose())
        Adj_train = Adj_train + Adj_eye[train_sample, :][:, train_sample]

        times = 0
        epochs = 0
        weight_list = nn.ParameterList()

        for l in range(args['layer_num']):

            # print('layer ' + str(l+1) + ' training:')
            feat_train = feat_train.to(torch.device('cpu')).numpy()

            start_time = time.time()
            feat_train = Adj_train.dot(feat_train)
            end_time = time.time()
            times = times + ( end_time - start_time )

            feat_train = torch.FloatTensor(feat_train)

            if l == 0:
                in_channel = args['feat_dim']
            else:
                in_channel = args['layer_output_dim'][l-1]
            hidden_channel = args['layer_output_dim'][l]
            out_channel = args['class_num']

            net_train = net.net_train(in_channel, hidden_channel, out_channel).to(torch.device(args['device']))
            optimizer = torch.optim.Adam(net_train.parameters(), lr=args['learning_rate'])
            batch = 0

            x = feat_train
            x_label = label_train

            while True:

                x = feat_train
                x_label = label_train

                x = x.to(torch.device(args['device']))
                x_label = x_label.to(torch.device(args['device']))

                start_time = time.time()
                optimizer.zero_grad()
                output = net_train(x)
                output = sigmoid(output)
                loss = loss_func(output, x_label)
                loss.backward()
                optimizer.step()
                end_time = time.time()
                times = times + ( end_time - start_time )

                ######

                if epochs % args['decision_step'] != 0:
                    epochs = epochs + 1
                    continue

                if epochs == 0:
                    loss_base = loss.detach()

                batch = batch + 1
                # print('batch', batch, 'loss:', loss) ###

                input_l2o = torch.zeros((1, args['layer_num']+1)).to(torch.device(args['device']))
                input_l2o[0] = loss.detach() - loss_base + 1 ###
                input_l2o[0, l+1] = 1
                input_l2o = input_l2o * 0.1

                if epochs == 0:
                    action, hx, cx = controller_l2o(input_l2o, 0, 0, 0, 0)
                else:
                    action, hx, cx = controller_l2o(input_l2o, action, hx, cx, int(epochs/args['decision_step']))

                ######

                if iround < args['init_round'] * 5:
                    action = predefine_action[iround, int(epochs/args['decision_step'])]
                epochs = epochs + 1

                if action == 1:

                    w = net_train.get_w()
                    w.requires_grad = False

                    _w = w.to(torch.device('cpu'))

                    start_time = time.time()
                    feat_train = torch.mm(feat_train, _w)
                    feat_train = relu(feat_train)
                    end_time = time.time()
                    times = times + ( end_time - start_time )

                    weight_list.append(w)

                    if l == args['layer_num'] - 1:
                        classifier = net_train.get_c()
                        classifier.requires_grad = False

                    break

        weight_list = weight_list.to(torch.device('cpu'))
        classifier = classifier.to(torch.device('cpu'))

        '''
        with torch.no_grad():
            output = net_test(feat_test, Adj_test, weight_list, classifier)[val]
            output = sigmoid(output)
            loss = loss_func(output, torch.tensor(labels[val]))
        '''

        neg_rewards = loss.detach().cuda() + epochs * args['time_ratio'] ###
        # print('loss: ', neg_rewards)
        baseline = args['baseline_ratio'] * baseline + (1 - args['baseline_ratio']) * neg_rewards
        neg_rewards = neg_rewards - baseline
        neg_rewards = sum( controller_l2o.get_selected_log_probs() ) * neg_rewards
        
        optimizer_l2o.zero_grad()
        neg_rewards.backward()
        optimizer_l2o.step()

    ###### finish learn to stop
    ##################################


    # dataset load
    feat_data, labels, Adj, dataset_split = dataset_load_func()
    print('Finished loading dataset.')

    # config parameter load
    with open('./config/' + config_file) as f:
        args = yaml.load(f)
    if not layer_num is None:
        args['layer_num'] = layer_num
    if not epoch_num is None:
        args['layer_train_batch'] = epoch_num

    result_file = './result/' + config_file[:-5] + '_l2o_' + str(args['layer_num']) + '_layer_' + str(seed) + '.npy'
    result_loss_data = []
    batch_each_layer = []
    result_prob_data = []

    num_nodes = args['node_num']

    # train, val and test set index
    train = dataset_split['train']
    train.sort()
    val = dataset_split['val']
    test = dataset_split['test']
    test.sort()
    print('trainset size', len(train),
          'testset size', len(test))

    # feature and label generate
    feat_train = torch.FloatTensor(feat_data)[train, :]
    label_train = labels[train]
    feat_test = torch.FloatTensor(feat_data)

    # Adj matrix generate
    Adj_eye = sps.eye(num_nodes, dtype=np.float32).tocsr()

    Adj_train = Adj[train, :][:, train]
    D_train = Adj_train.sum(axis=0)
    Adj_train = Adj_train.multiply(1/D_train.transpose())
    Adj_train = Adj_train + Adj_eye[train, :][:, train]

    Adj_test = Adj
    D_test = Adj_test.sum(axis=0)
    Adj_test = Adj_test.multiply(1/D_test.transpose())
    Adj_test = Adj_test + Adj_eye

    print('Finished generating adj matrix.')

    # layerwise training
    times = 0
    epochs = 0
    weight_list = nn.ParameterList()

    for l in range(args['layer_num']):

        print('layer ' + str(l+1) + ' training:')

        feat_train = feat_train.to(torch.device('cpu')).numpy()

        start_time = time.time()
        feat_train = Adj_train.dot(feat_train)
        end_time = time.time()
        times = times + ( end_time - start_time )

        feat_train = torch.FloatTensor(feat_train)

        feeder_train = utils.feeder(feat_train, label_train)
        dataset_train = torch.utils.data.DataLoader(dataset=feeder_train, batch_size=args['batch_size'], shuffle=True)

        if l == 0:
            in_channel = args['feat_dim']
        else:
            in_channel = args['layer_output_dim'][l-1]
        hidden_channel = args['layer_output_dim'][l]
        out_channel = args['class_num']

        net_train = net.net_train(in_channel, hidden_channel, out_channel).to(torch.device(args['device']))
        optimizer = torch.optim.Adam(net_train.parameters(), lr=args['learning_rate'])
        batch = 0
        while True:
            for x, x_label in dataset_train:

                x = x.to(torch.device(args['device']))
                x_label = x_label.to(torch.device(args['device']))

                start_time = time.time()
                optimizer.zero_grad()
                output = net_train(x)
                output = sigmoid(output)
                loss = loss_func(output, x_label)
                loss.backward()
                optimizer.step()
                end_time = time.time()
                times = times + ( end_time - start_time )

            result_loss_data.append(loss.data.cpu().numpy())

            batch = batch + 1
            print('batch', batch, 'loss:', loss.data)

            if epochs % args['decision_step'] != 0:
                epochs = epochs + 1
                continue

            if epochs == 0:
                loss_base = loss.detach()

            input_l2o = torch.zeros((1, args['layer_num']+1)).to(torch.device(args['device']))
            input_l2o[0] = loss.detach() - loss_base + 1
            input_l2o[0, l+1] = 1
            input_l2o = input_l2o * 0.1

            if epochs == 0:
                action, hx, cx = controller_l2o(input_l2o, 0, 0, 0, 0)
            else:
                action, hx, cx = controller_l2o(input_l2o, action, hx, cx, int(epochs/args['decision_step']))

            epochs = epochs + 1
            action = 0

            result_prob_data.append(controller_l2o.get_stop_prob().data.cpu().numpy())

            if controller_l2o.get_stop_prob() >= args['stop_prob_threshold'] and batch > 175 or batch > 475:

                action = 1
                batch_each_layer.append(batch)
                w = net_train.get_w()
                w.requires_grad = False

                if l != args['layer_num'] - 1:
                    _w = w.to(torch.device('cpu'))

                    start_time = time.time()
                    feat_train = torch.mm(feat_train, _w)
                    feat_train = relu(feat_train)
                    end_time = time.time()
                    times = times + ( end_time - start_time )

                weight_list.append(w)
                if l == args['layer_num'] - 1:
                    classifier = net_train.get_c()
                    classifier.requires_grad = False
                break

    os.system('nvidia-smi')

    weight_list = weight_list.to(torch.device('cpu'))
    classifier = classifier.to(torch.device('cpu'))

    # np.save(result_file, np.array(result_loss_data))

    # test
    net_test = net.net_test()
    with torch.no_grad():
        output = net_test(feat_test, Adj_test, weight_list, classifier)
        output[output>0] = 1
        output[output<=0] = 0
        output_val = output[val]
        output_test = output[test]

    print("accuracy in val:", f1_score(labels[val], output_val.data.numpy(), average="micro"))
    print("accuracy in test:", f1_score(labels[test], output_test.data.numpy(), average="micro"))
    print("average epoch time:", times / epochs)
    print("total time:", times)

    return f1_score(labels[test], output_test.data.numpy(), average="micro"), sum(batch_each_layer), times, np.array(batch_each_layer)


def parser_loader():

    parser = argparse.ArgumentParser(description='L2O_LWGCN')
    parser.add_argument('--config-file', type=str, default='ppi.yaml')
    parser.add_argument('--dataset', type=str, default='ppi')
    parser.add_argument('--layer-num', type=int, default=3)
    parser.add_argument('--epoch-num', nargs='+', type=int, default=None)
    parser.add_argument('--controller-len', type=int, default=None)

    return parser

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True

if __name__ == "__main__":

    # os.environ['CUDA_VISIBLE_DEVICES'] = '1'

    parser = parser_loader()
    args = vars(parser.parse_args())
    print(args)

    acc = np.zeros(1)
    epoch_sum = np.zeros(1)
    times = np.zeros(1)
    epoch_array = np.zeros((args['layer_num'], 1))

    for seed in range(1):
        setup_seed(seed)
        acc[seed], epoch_sum[seed], times[seed], epoch_array[:, seed] = run_l2o(utils.ppi_loader, seed, **args)

    print('')
    print(np.mean(acc), np.mean(epoch_sum), np.mean(times))
    print(np.std(acc), np.std(epoch_sum), np.std(times))

