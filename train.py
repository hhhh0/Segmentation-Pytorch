import os, sys
import torch
import torch.nn as nn
import timeit
import math
import torch.backends.cudnn as cudnn
from argparse import ArgumentParser
# user
from builders.model_builder import build_model
from builders.dataset_builder import build_dataset_train, build_dataset_test
from builders.loss_builder import build_loss
from builders.validation_builder import predict_sliding, predict_whole
from utils.utils import setup_seed, init_weight, netParams
from utils.scheduler.lr_scheduler import PolyLR, WarmupPolyLR
from utils.plot_log import draw_log
from utils.record_log import record_log
from utils.earlyStopping import EarlyStopping
from tqdm import tqdm

sys.setrecursionlimit(1000000)  # solve problem 'maximum recursion depth exceeded'
GLOBAL_SEED = 88


def train(args, train_loader, model, criterion, optimizer, epoch):
    """
    args:
       train_loader: loaded for training dataset
       model: model
       criterion: loss function
       optimizer: optimization algorithm, such as ADAM or SGD
       epoch: epoch number
    return: average loss, per class IoU, and mean IoU
    """

    model.train()
    epoch_loss = []
    lr = optimizer.param_groups[0]['lr']
    total_batches = len(train_loader)
    pbar = tqdm(iterable=enumerate(train_loader), total=total_batches,
                desc='Epoch {}/{}'.format(epoch, args.max_epochs))
    for iteration, batch in pbar:
        images, labels, _, _ = batch
        images = images.cuda()
        labels = labels.long().cuda()
        if args.model == 'PSPNet50':
            x, aux = model(images)
            main_loss = criterion(x, labels)
            aux_loss = criterion(aux, labels)
            loss = 0.6 * main_loss + 0.4 * aux_loss
        else:
            output = model(images)
            if type(output) is tuple:
                output = output[0]
            loss = criterion(output, labels)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        epoch_loss.append(loss.item())

    average_epoch_loss_train = sum(epoch_loss) / len(epoch_loss)
    return average_epoch_loss_train, lr


def main(args):
    """
    args:
       args: global arguments
    """
    print(args)
    # set the seed
    setup_seed(GLOBAL_SEED)
    cudnn.enabled = True
    cudnn.benchmark = True  # 寻找最优配置
    cudnn.deterministic = True  # 减少波动
    torch.cuda.empty_cache()  # 清空显卡缓存

    # build the model and initialization weights
    model = build_model(args.model, num_classes=args.classes)
    init_weight(model, nn.init.kaiming_normal_, nn.BatchNorm2d, 1e-3, 0.1, mode='fan_in')

    # load train set and data augmentation
    datas, trainLoader = build_dataset_train(args.dataset, args.input_size, args.batch_size, args.train_type,
                                             args.random_scale, args.random_mirror, args.num_workers)
    # load the test set, if want set cityscapes test dataset change none_gt=False
    testLoader, class_dict_df = build_dataset_test(args.dataset, args.num_workers, sliding=args.sliding, none_gt=True)

    print("the number of parameters: %d ==> %.2f M" % (netParams(model), (netParams(model) / 1e6)))
    print('Dataset statistics')
    print("data['classWeights']: ", datas['classWeights'])
    print('mean and std: ', datas['mean'], datas['std'])

    # define loss function, respectively
    criteria = build_loss(args, None, ignore_label)

    # define optimization strategy
    if args.optim == 'sgd':
        optimizer = torch.optim.SGD(
            filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr, momentum=0.9, weight_decay=1e-4)
    elif args.optim == 'adam':
        optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr, betas=(0.9, 0.999), eps=1e-08,
            weight_decay=1e-4)

    # learning scheduling, for 20 epoch lr*0.9
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=20, gamma=0.9)

    # move model and criteria on cuda
    if args.cuda:
        print("use gpu id: '{}'".format(args.gpus))
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus
        criteria = criteria.cuda()
        if torch.cuda.device_count() > 1:
            print("torch.cuda.device_count()=", torch.cuda.device_count())
            args.gpu_nums = torch.cuda.device_count()
            model = nn.DataParallel(model).cuda()
        else:
            args.gpu_nums = 1
            print("single GPU for training")
            model = model.cuda()
        if not torch.cuda.is_available():
            raise Exception("No GPU found or Wrong gpu id, please run without --cuda")

    # initial log file val output save
    args.savedir = (args.savedir + args.dataset + '/' + args.model + 'bs'
                    + str(args.batch_size) + 'gpu' + str(args.gpu_nums) + "_" + str(args.train_type) + '/')
    if not os.path.exists(args.savedir):
        os.makedirs(args.savedir)

    # save_seg_dir
    args.save_seg_dir = os.path.join(args.savedir, 'predict_sliding')
    if not os.path.exists(args.save_seg_dir):
        os.makedirs(args.save_seg_dir)

    recorder = record_log(args)
    recorder.record_args(datas, netParams(model), GLOBAL_SEED)

    # initialize the early_stopping object
    early_stopping = EarlyStopping(patience=50)

    start_epoch = 1
    lossTr_list = []
    mIOU_val_list = []
    lossVal_list = []
    mIOU_val = 0
    # continue training
    if args.resume:
        logger, lines = recorder.resume_logfile()
        for index, line in enumerate(lines):
            lossTr_list.append(float(line.strip().split()[2]))
            if ((index + 1) % args.val_epochs) == 0 or index == 0:
                lossVal_list.append(float(line.strip().split()[3]))
                mIOU_val_list.append(float(line.strip().split()[5]))
        if os.path.isfile(args.resume):
            checkpoint = torch.load(args.resume)
            start_epoch = checkpoint['epoch'] + 1
            model.load_state_dict(checkpoint['model'])
            print("loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))
        else:
            print("no checkpoint found at '{}'".format(args.resume))
    else:
        logger = recorder.initial_logfile()
        logger.flush()
    print('>>>>>>>>>>>beginning training>>>>>>>>>>>')
    for epoch in range(start_epoch, args.max_epochs + 1):
        # training
        lossTr, lr = train(args, trainLoader, model, criteria, optimizer, epoch)
        lossTr_list.append(lossTr)

        # validation if mode==validation, predict with label; else mode==test predict without label.
        if epoch % args.val_epochs == 0 or epoch == 1 or epoch == args.max_epochs:
            if args.sliding:
                val_loss, FWIoU, mIOU_val, per_class_iu, pa, cpa, mpa = predict_sliding(args, model, testLoader,
                                                                                        args.input_size,
                                                                                        criteria, mode='validation')
            else:
                val_loss, FWIoU, mIOU_val, per_class_iu = predict_whole()
            mIOU_val_list.append(mIOU_val)
            lossVal_list.append(val_loss.item())
            # record trainVal information
            recorder.record_trainVal_log(logger, epoch, lr, lossTr, val_loss, FWIoU, mIOU_val, per_class_iu, pa,
                                         mpa, cpa, class_dict_df)
        else:
            # record train information
            recorder.record_train_log(logger, epoch, lr, lossTr)

        # Update lr_scheduler. In pytorch 1.1.0 and later, should call 'optimizer.step()' before 'lr_scheduler.step()'
        lr_scheduler.step()

        # draw log fig
        draw_log(args, epoch, mIOU_val_list, lossVal_list)

        # save the model
        model_file_name = args.savedir + '/model_' + str(epoch) + '.pth'
        state = {"epoch": epoch, "model": model.state_dict()}
        if epoch > args.max_epochs - 10:
            torch.save(state, model_file_name)
        elif epoch % 10 == 0:
            torch.save(state, model_file_name)

        # early_stopping monitor
        early_stopping.monitor(monitor=mIOU_val)
        if early_stopping.early_stop:
            if not os.path.exists(model_file_name):
                torch.save(state, model_file_name)
                val_loss, FWIoU, mIOU_val, per_class_iu, pa, cpa, mpa = predict_sliding(args, model, testLoader,
                                                                                        args.input_size,
                                                                                        criteria, mode='validation')
                print(
                    "Epoch  %d\tlr= %.6f\tTrain Loss = %.4f\tVal Loss = %.4f\tmIOU(val) = %.4f\tper_class_iu= %s\n" % (
                        epoch, lr, lossTr, val_loss, mIOU_val, str(per_class_iu)))
            break

    print("Early stopping and Save checkpoint")
    logger.close()


def parse_args():
    parser = ArgumentParser(description='Efficient semantic segmentation')
    # model and dataset
    parser.add_argument('--model', type=str, default="DualSeg_res50", help="model name")
    parser.add_argument('--dataset', type=str, default="paris", help="dataset: cityscapes or camvid")
    parser.add_argument('--input_size', type=str, default=(256, 256), help="input size of model")
    parser.add_argument('--num_workers', type=int, default=4, help=" the number of parallel threads")
    parser.add_argument('--train_type', type=str, default="train",
                        help="ontrain for training on train set, ontrainval for training on train+val set")
    # training hyper params
    parser.add_argument('--max_epochs', type=int, default=300,
                        help="the number of epochs: 300 for train set, 350 for train+val set")
    parser.add_argument('--batch_size', type=int, default=4, help="the batch size is set to 16 for 2 GPUs")
    parser.add_argument('--val_epochs', type=int, default=10,
                        help="the number of epochs: 100 for val set")
    parser.add_argument('--random_mirror', type=bool, default=True, help="input image random mirror")
    parser.add_argument('--random_scale', type=bool, default=True, help="input image resize 0.5 to 2")
    parser.add_argument('--lr', type=float, default=1e-3, help="initial learning rate")
    parser.add_argument('--optim', type=str.lower, default='adam', choices=['sgd', 'adam'], help="select optimizer")
    parser.add_argument('--sliding', type=bool, default=True, help="sliding predict mode")
    parser.add_argument('--loss', type=str, default="CrossEntropyLoss2d",
                        choices=['CrossEntropyLoss2d', 'ProbOhemCrossEntropy2d', 'CrossEntropyLoss2dLabelSmooth',
                                 'LovaszSoftmax', 'FocalLoss2d'], help="choice loss for train or val in list")
    # cuda setting
    parser.add_argument('--cuda', type=bool, default=True, help="running on CPU or GPU")
    parser.add_argument('--gpus', type=str, default="0", help="default GPU devices (0,1)")
    # checkpoint and log
    parser.add_argument('--resume', type=str, default="",
                        help="use this file to load last checkpoint for continuing training")
    parser.add_argument('--savedir', default="./checkpoint/", help="directory to save the model snapshot")
    parser.add_argument('--logFile', default="log.txt", help="storing the training and validation logs")
    args = parser.parse_args()

    return args


if __name__ == '__main__':
    start = timeit.default_timer()
    args = parse_args()

    if args.dataset == 'cityscapes':
        args.classes = 19
        args.input_size = (512, 512)
        ignore_label = 255
    elif args.dataset == 'camvid':
        args.classes = 11
        args.input_size = (360, 480)
        ignore_label = 11
    elif args.dataset == 'paris':
        args.classes = 3
        args.input_size = (512, 512)
        ignore_label = 255
    elif args.dataset == 'road':
        args.classes = 2
        args.input_size = (512, 512)
        ignore_label = 255
    elif args.dataset == 'ai':
        args.classes = 8
        args.input_size = (256, 256)
        ignore_label = 255
    else:
        raise NotImplementedError(
            "This repository now supports datasets %s is not included" % args.dataset)

    main(args)
    end = timeit.default_timer()
    hour = 1.0 * (end - start) / 3600
    minute = (hour - int(hour)) * 60
    print("training time: %d hour %d minutes" % (int(hour), int(minute)))
