import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import SGD, lr_scheduler
from sklearn.metrics.cluster import normalized_mutual_info_score as nmi_score
from sklearn.metrics import adjusted_rand_score as ari_score
from sklearn.cluster import KMeans
from utils.util import BCE, PairEnum, cluster_acc, Identity, AverageMeter, seed_torch
from utils import ramps 
from models.resnet import ResNet, BasicBlock
from data.cifarloader import CIFAR10Loader, CIFAR10LoaderMix, CIFAR100Loader, CIFAR100LoaderMix, CIFAR10LoaderTwoStream, CIFAR10Loader_iter
from data.svhnloader import SVHNLoader, SVHNLoaderMix
from tqdm import tqdm
import numpy as np
import os
import faiss

from loss import CrossEntropyLabelSmooth

# os.environ["CUDA_VISIBLE_DEVICES"] = '1'

def smooth_loss(feat,mask_lb):

    feature = (feat[~mask_lb])
    feature = F.normalize(feature,1)
    inputs = feature.mm(feature.t())
    targets = smooth_hot(inputs.detach().clone())
    outputs = F.log_softmax(inputs, dim=1)
    loss = - (targets * outputs)
    loss = loss.sum(dim=1)
    loss = loss.mean(dim=0)
    return loss

def smooth_hot(inputs, k=6):
    # Sort
    targets = torch.arange(inputs.size(0)).cuda().detach()

    _, index_sorted = torch.sort(inputs, dim=1, descending=True)

    ones_mat = torch.ones(targets.size(0), k).cuda()#.to(self.device)
    targets = torch.unsqueeze(targets, 1)
    targets_onehot = -torch.ones(inputs.size()).cuda()#.to(self.device)

    weights = F.softmax(ones_mat, dim=1)
    targets_onehot.scatter_(1, index_sorted[:, 0:k], ones_mat * weights)
    targets_onehot.scatter_(1, targets, float(1))
    return targets_onehot

def rank_bce(criterion2, feat, mask_lb, prob2, prob2_bar):
    rank_feat = (feat[~mask_lb]).detach()


    rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
    rank_idx1, rank_idx2 = PairEnum(rank_idx)
    rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]
    rank_idx1, _ = torch.sort(rank_idx1, dim=1)
    rank_idx2, _ = torch.sort(rank_idx2, dim=1)
    rank_diff = rank_idx1 - rank_idx2
    rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
    target_ulb = torch.ones_like(rank_diff).float().to(device)
    target_ulb[rank_diff > 0] = -1

    prob1_ulb, _ = PairEnum(prob2[~mask_lb])
    _, prob2_ulb = PairEnum(prob2_bar[~mask_lb])
    loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
    return loss_bce

def _update_ema_variables(model, ema_model, alpha, global_step):
    alpha = min(1 - 1 / (global_step + 1), alpha)
    # for ema_param, param in zip(ema_model.parameters(), model.parameters()):
    #    ema_param.data.mul_(alpha).add_(1 - alpha, param.data)

    for (ema_name, ema_param), (model_name, param) in zip(ema_model.named_parameters(), model.named_parameters()):
        ema_param.data.mul_(alpha).add_(1 - alpha, param.data)

def train(model, model_ema, labeled_eval_loader_train, unlabeled_eval_loader_test, unlabeled_eval_loader_train, args):


    # args.head = 'head2'
    # feats = test(model_ema, unlabeled_eval_loader_train, args)
    # feats = F.normalize(torch.cat(feats,dim=0), dim=1)
    # cluster = faiss.Kmeans(512, 5, niter=300, verbose=True, gpu=True)
    # moving_avg_features = feats.numpy()
    # cluster.train(moving_avg_features)
    # _, labels = cluster.index.search(moving_avg_features, 1)
    # target_label = labels.reshape(-1).tolist()
    #
    # unlabeled_train_loader = CIFAR10Loader_iter(root=args.dataset_root, batch_size=args.batch_size // 2, split='train',
    #                                             aug='twice', shuffle=True, target_list=range(args.num_labeled_classes, num_classes),new_labels=target_label)

    labeled_train_loader = CIFAR10Loader_iter(root=args.dataset_root, batch_size=args.batch_size // 2, split='train',
                                              aug='twice', shuffle=True, target_list=range(args.num_labeled_classes))
    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss() 
    criterion2 = BCE()
    criterion3 = CrossEntropyLabelSmooth(num_classes=args.num_unlabeled_classes)
    for epoch in range(args.epochs):
        loss_record = AverageMeter()
        model.train()
        model_ema.train()
        exp_lr_scheduler.step()
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        iters = 400

        if epoch % 5 == 0:
            args.head = 'head2'
            feats, _ = test(model_ema, unlabeled_eval_loader_train, args)
            feats = F.normalize(torch.cat(feats, dim=0), dim=1)
            cluster = faiss.Kmeans(512, 5, niter=300, verbose=True, gpu=True)
            moving_avg_features = feats.numpy()
            cluster.train(moving_avg_features)
            target_centers, labels = cluster.index.search(moving_avg_features, 1)
            labels= labels+5
            target_label = labels.reshape(-1).tolist()
        
            #centers=faiss.vector_to_array(cluster.centroids).reshape(5, 512)
            centers = cluster.centroids
            model.memory.prototypes[args.num_labeled_classes:] = F.normalize(torch.tensor(centers).cuda(), dim=1)
            model_ema.memory.prototypes[args.num_labeled_classes:] = F.normalize(torch.tensor(centers).cuda(), dim=1)

            feats, labels = test(model_ema, labeled_eval_loader_train, args)
            feats = F.normalize(torch.cat(feats,dim=0), dim=1)
            centers = torch.zeros(args.num_labeled_classes, 512)
            for i in range(args.num_labeled_classes):
                idx = torch.where(torch.tensor(labels)==i)[0]
                centers[i] = torch.mean(feats[idx],0)
            model.memory.prototypes[:args.num_labeled_classes] = torch.tensor(centers).cuda()
            model_ema.memory.prototypes[:args.num_labeled_classes] = torch.tensor(centers).cuda()

            unlabeled_train_loader = CIFAR10Loader_iter(root=args.dataset_root, batch_size=args.batch_size // 2,
                                                        split='train',
                                                        aug='twice', shuffle=True,
                                                        target_list=range(args.num_labeled_classes, num_classes),
                                                        new_labels=target_label)
            # model.head2.weight.data.copy_(
            #     torch.from_numpy(F.normalize(target_centers, axis=1)).float().cuda())

        # labeled_train_loader.new_epoch()
        # unlabeled_train_loader.new_epoch()
        # for batch_idx,_ in enumerate(range(iters)):
        #     ((x_l, x_bar_l), label_l, idx) = labeled_train_loader.next()
        #     ((x_u, x_bar_u), label_u, idx) = unlabeled_train_loader.next()
        for batch_idx, (((x_l, x_bar_l), label_l, idx),((x_u, x_bar_u), label_u, idx)) in enumerate(zip(labeled_train_loader,unlabeled_train_loader)):

            x = torch.cat([x_l,x_u],dim=0)
            x_bar = torch.cat([x_bar_l,x_bar_u],dim=0)
            label = torch.cat([label_l,label_u],dim=0)
            idx = torch.cat([idx,idx],dim=0)

            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)

            output1, output2, feat = model(x)
            output1_bar, output2_bar, _ = model(x_bar)

            with torch.no_grad():
                output1_ema, output2_ema, feat_ema = model_ema(x)
                output1_bar_ema, output2_bar_ema, _ = model_ema(x_bar)
            prob1, prob1_bar, prob2, prob2_bar = F.softmax(output1, dim=1),  F.softmax(output1_bar, dim=1), F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)
            prob1_ema, prob1_bar_ema, prob2_ema, prob2_bar_ema = F.softmax(output1_ema, dim=1),  F.softmax(output1_bar_ema, dim=1), F.softmax(output2_ema, dim=1), F.softmax(output2_bar_ema, dim=1)

            mask_lb = label<args.num_labeled_classes

            loss_ce_label   = criterion1(output1[mask_lb], label[mask_lb])
            loss_ce_unlabel = criterion1(output2[~mask_lb], label[~mask_lb])#torch.tensor(0)#

            loss_ce = loss_ce_label + loss_ce_unlabel

            loss_bce = rank_bce(criterion2,feat,mask_lb,prob2,prob2_bar)#torch.tensor(0)#

            consistency_loss = F.mse_loss(prob1, prob1_bar) + F.mse_loss(prob2, prob2_bar)
            consistency_loss_ema = F.mse_loss(prob1, prob1_bar_ema) + F.mse_loss(prob2, prob2_bar_ema)

            loss = loss_ce + loss_bce + w * consistency_loss + w * consistency_loss_ema #+ smooth_loss(feat,mask_lb) #+ MCR(feat, idx)

            loss_record.update(loss.item(), x.size(0))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            _update_ema_variables(model, model_ema, 0.99, epoch * iters + batch_idx)

            if batch_idx%200==0:
                print('Train Epoch: {}, iter {}/{} unl-CE Loss: {:.4f}, l-CE Loss: {:.4f}, BCE Loss: {:.4f}, CL Loss: {:.4f}, Avg Loss: {:.4f}'
                      .format(epoch, batch_idx, 400, loss_ce_unlabel.item(), loss_ce_label.item(), loss_bce.item(), consistency_loss.item(), loss_record.avg))
        print('Train Epoch: {} Avg Loss: {:.4f}'.format(epoch, loss_record.avg))
        # print('test on labeled classes')
        # args.head = 'head1'
        # test(model, labeled_eval_loader_test, args)


        # print('test on unlabeled classes')
        args.head= 'head2'
        # test(model, unlabeled_eval_loader_train, args)
        test(model_ema, unlabeled_eval_loader_test, args)

def train_IL(model, train_loader, labeled_eval_loader, unlabeled_eval_loader, args):
    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss() 
    criterion2 = BCE() 
    for epoch in range(args.epochs):
        loss_record = AverageMeter()
        model.train()
        exp_lr_scheduler.step()
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)
        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            output1, output2, feat = model(x)
            output1_bar, output2_bar, _ = model(x_bar)
            prob1, prob1_bar, prob2, prob2_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1), F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)
            
            mask_lb = label < args.num_labeled_classes

            rank_feat = (feat[~mask_lb]).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]
            
            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            prob1_ulb, _ = PairEnum(prob2[~mask_lb])
            _, prob2_ulb = PairEnum(prob2_bar[~mask_lb])

            loss_ce = criterion1(output1[mask_lb], label[mask_lb])

            label[~mask_lb] = (output2[~mask_lb]).detach().max(1)[1] + args.num_labeled_classes

            loss_ce_add = w * criterion1(output1[~mask_lb], label[~mask_lb]) / args.rampup_coefficient * args.increment_coefficient
            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob1, prob1_bar) + F.mse_loss(prob2, prob2_bar)

            loss = loss_ce + loss_bce + loss_ce_add + w * consistency_loss

            loss_record.update(loss.item(), x.size(0))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        print('Train Epoch: {} Avg Loss: {:.4f}'.format(epoch, loss_record.avg))
        print('test on labeled classes')
        args.head = 'head1'
        test(model, labeled_eval_loader, args)
        print('test on unlabeled classes')
        args.head='head2'
        test(model, unlabeled_eval_loader, args)

def test(model, test_loader, args):
    model.eval()
    preds=np.array([])
    targets=np.array([])
    feats=[]
    for batch_idx, (x, label, _) in enumerate(tqdm(test_loader)):
        x, label = x.to(device), label.to(device)
        output1, output2, feat= model(x)
        if args.head=='head1':
            output = output1
        else:
            output = output2
        _, pred = output.max(1)
        targets=np.append(targets, label.cpu().numpy())
        preds=np.append(preds, pred.cpu().numpy())
        feats.append(feat.data.cpu())
    acc, nmi, ari = cluster_acc(targets.astype(int), preds.astype(int)), nmi_score(targets, preds), ari_score(targets, preds) 
    print('Test acc {:.4f}, nmi {:.4f}, ari {:.4f}'.format(acc, nmi, ari))
    return feats, targets

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
            description='cluster',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--gamma', type=float, default=0.1)
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight_decay', type=float, default=1e-4)
    parser.add_argument('--epochs', default=200, type=int)
    parser.add_argument('--rampup_length', default=150, type=int)
    parser.add_argument('--rampup_coefficient', type=float, default=50)
    parser.add_argument('--increment_coefficient', type=float, default=0.05)
    parser.add_argument('--step_size', default=170, type=int)
    parser.add_argument('--batch_size', default=128, type=int)
    parser.add_argument('--num_unlabeled_classes', default=5, type=int)
    parser.add_argument('--num_labeled_classes', default=5, type=int)
    parser.add_argument('--dataset_root', type=str, default='./data/datasets/CIFAR/')
    parser.add_argument('--exp_root', type=str, default='./data/experiments_tmp/')
    parser.add_argument('--warmup_model_dir', type=str, default='./data/experiments/pretrained/supervised_learning/resnet_rotnet_cifar10.pth')
    parser.add_argument('--topk', default=5, type=int)
    parser.add_argument('--IL', action='store_true', default=False, help='w/ incremental learning')
    parser.add_argument('--model_name', type=str, default='resnet')
    parser.add_argument('--dataset_name', type=str, default='cifar10', help='options: cifar10, cifar100, svhn')
    parser.add_argument('--seed', default=1, type=int)
    parser.add_argument('--mode', type=str, default='train')
    args = parser.parse_args()
    args.cuda = torch.cuda.is_available()
    device = torch.device("cuda" if args.cuda else "cpu")
    seed_torch(args.seed)
    runner_name = os.path.basename(__file__).split(".")[0]
    model_dir= os.path.join(args.exp_root, runner_name)
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    args.model_dir = model_dir+'/'+'{}.pth'.format(args.model_name) 

    model = ResNet(BasicBlock, [2, 2, 2, 2], args.num_labeled_classes, args.num_unlabeled_classes).to(device)
    model_ema = ResNet(BasicBlock, [2, 2, 2, 2], args.num_labeled_classes, args.num_unlabeled_classes).to(device)

    num_classes = args.num_labeled_classes + args.num_unlabeled_classes

    if args.mode=='train':
        state_dict = torch.load(args.warmup_model_dir)
        model.load_state_dict(state_dict, strict=False)
        model_ema.load_state_dict(state_dict, strict=False)

        for name, param in model.named_parameters():
            if 'head' not in name and 'layer4' not in name:
                param.requires_grad = False

    unlabeled_eval_loader_train = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug=None , shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
    unlabeled_eval_loader_test = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='test' , aug=None , shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
    labeled_eval_loader_test   = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='test' , aug=None , shuffle=False, target_list = range(args.num_labeled_classes))
    labeled_eval_loader_train = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug=None , shuffle=False, target_list = range(args.num_labeled_classes))

    if args.mode == 'train':
        train(model, model_ema, labeled_eval_loader_train, unlabeled_eval_loader_test, unlabeled_eval_loader_train, args)
        torch.save(model.state_dict(), args.model_dir)
        print("model saved to {}.".format(args.model_dir))
    else:
        print("model loaded from {}.".format(args.model_dir))
        model.load_state_dict(torch.load(args.model_dir))

    print('Evaluating on Head1')
    args.head = 'head1'
    print('test on labeled classes (test split)')
    test(model, labeled_eval_loader_test, args)
    print('Evaluating on Head2')
    args.head = 'head2'
    print('test on unlabeled classes (train split)')
    test(model, unlabeled_eval_loader_train, args)
    print('test on unlabeled classes (test split)')
    test(model, unlabeled_eval_loader_test, args)