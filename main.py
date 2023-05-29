#! /usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2020 Imperial College London (Pingchuan Ma)
# Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

""" TCN for lipreading"""

import os
import time
import random
import argparse
import numpy as np
from tqdm import tqdm
os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'

import torch
import torch.nn as nn
import torch.nn.functional as F
import matplotlib.pyplot as plt
import soundfile as sf
from torchmetrics.audio.stoi import ShortTimeObjectiveIntelligibility
import torchaudio.transforms as transforms
import librosa


from lipreading.utils import get_save_folder
from lipreading.utils import load_json, save2npz
from lipreading.utils import load_model, CheckpointSaver
from lipreading.utils import get_logger, update_logger_batch
from lipreading.utils import showLR, calculateNorm2, AverageMeter
from lipreading.model import Lipreading, AVLipreading
from lipreading.mixup import mixup_data, mixup_criterion
from lipreading.optim_utils import get_optimizer, CosineScheduler
from lipreading.dataloaders import get_data_loaders, get_preprocessing_pipelines, unit_test_data_loader
from lipreading.dataset import mel_transform

# gpu_num = torch.cuda.device_count()
gpu_num = 1

def load_args(default_config=None):
    parser = argparse.ArgumentParser(description='Pytorch Lipreading ')
    # -- dataset config
    parser.add_argument('--dataset', default='lrw', help='dataset selection')
    parser.add_argument('--num-classes', type=int, default=500, help='Number of classes')
    parser.add_argument('--modality', default='video', choices=['video', 'audio','av'], help='choose the modality')
    # -- directory
    parser.add_argument('--data-dir', default='./datasets/LRW_h96w96_mouth_crop_gray', help='Loaded data directory')
    parser.add_argument('--label-path', type=str, default='./labels/500WordsSortedList.txt', help='Path to txt file with labels')
    parser.add_argument('--annonation-direc', default=None, help='Loaded data directory')
    # -- model config
    parser.add_argument('--backbone-type', type=str, default='resnet', choices=['resnet', 'shufflenet'], help='Architecture used for backbone')
    parser.add_argument('--relu-type', type=str, default='relu', choices=['relu','prelu'], help='what relu to use' )
    parser.add_argument('--width-mult', type=float, default=1.0, help='Width multiplier for mobilenets and shufflenets')
    # -- TCN config
    parser.add_argument('--tcn-kernel-size', type=int, nargs="+", help='Kernel to be used for the TCN module')
    parser.add_argument('--tcn-num-layers', type=int, default=4, help='Number of layers on the TCN module')
    parser.add_argument('--tcn-dropout', type=float, default=0.2, help='Dropout value for the TCN module')
    parser.add_argument('--tcn-dwpw', default=False, action='store_true', help='If True, use the depthwise seperable convolution in TCN architecture')
    parser.add_argument('--tcn-width-mult', type=int, default=1, help='TCN width multiplier')
    # -- DenseTCN config
    parser.add_argument('--densetcn-block-config', type=int, nargs = "+", help='number of denselayer for each denseTCN block')
    parser.add_argument('--densetcn-kernel-size-set', type=int, nargs = "+", help='kernel size set for each denseTCN block')
    parser.add_argument('--densetcn-dilation-size-set', type=int, nargs = "+", help='dilation size set for each denseTCN block')
    parser.add_argument('--densetcn-growth-rate-set', type=int, nargs = "+", help='growth rate for DenseTCN')
    parser.add_argument('--densetcn-dropout', default=0.2, type=float, help='Dropout value for DenseTCN')
    parser.add_argument('--densetcn-reduced-size', default=256, type=int, help='the feature dim for the output of reduce layer')
    parser.add_argument('--densetcn-se', default = False, action='store_true', help='If True, enable SE in DenseTCN')
    parser.add_argument('--densetcn-condense', default = False, action='store_true', help='If True, enable condenseTCN')
    # -- attention config
    parser.add_argument('--attention-embed-dim', type=int, default = 1664,  help='Attention layer input embedding size')
    parser.add_argument('--attention-num-head', type=int, default = 8, help='Attention layer head num')
    parser.add_argument('--attention-dropout', type=float, default = 0.2, help='Attention layer dropout')
    # -- train
    parser.add_argument('--training-mode', default='tcn', help='tcn')
    parser.add_argument('--batch-size', type=int, default=32, help='Mini-batch size')
    parser.add_argument('--optimizer',type=str, default='adamw', choices = ['adam','sgd','adamw'])
    parser.add_argument('--lr', default=3e-4, type=float, help='initial learning rate')
    parser.add_argument('--init-epoch', default=0, type=int, help='epoch to start at')
    parser.add_argument('--epochs', default=80, type=int, help='number of epochs')
    parser.add_argument('--test', default=False, action='store_true', help='training mode')
    # -- mixup
    parser.add_argument('--alpha', default=0.4, type=float, help='interpolation strength (uniform=1., ERM=0.)')
    # -- test
    parser.add_argument('--model-path', type=str, default=None, help='Pretrained model pathname')
    parser.add_argument('--allow-size-mismatch', default=False, action='store_true',
                        help='If True, allows to init from model with mismatching weight tensors. Useful to init from model with diff. number of classes')
    # -- feature extractor
    parser.add_argument('--extract-feats', default=False, action='store_true', help='Feature extractor')
    parser.add_argument('--mouth-patch-path', type=str, default=None, help='Path to the mouth ROIs, assuming the file is saved as numpy.array')
    parser.add_argument('--mouth-embedding-out-path', type=str, default=None, help='Save mouth embeddings to a specificed path')
    # -- json pathname
    parser.add_argument('--config-path', type=str, default="./configs/lrw_resnet18_dctcn.json", help='Model configuration with json format')
    # -- other vars
    parser.add_argument('--interval', default=50, type=int, help='display interval')
    parser.add_argument('--workers', default=8, type=int, help='number of data loading workers')
    # paths
    parser.add_argument('--logging-dir', type=str, default='./train_logs', help = 'path to the directory in which to save the log file')
    # use boundaries
    parser.add_argument('--use-boundary', default=False, action='store_true', help='include hard border at the testing stage.')
    # -- Spectrogram config
    parser.add_argument('--spectrogram-hop-length', type=int, default=145, help='hop length of spectrogram')
    parser.add_argument('--spectrogram-n-fft', type=int, default=2048, help='n_fft for making spectrogram')
    parser.add_argument('--spectrogram-sample-rate', type=int, default=16000, help='sampling rate of spectrogram')


    args = parser.parse_args()
    return args


args = load_args()

torch.manual_seed(1)
np.random.seed(1)
random.seed(1)
torch.backends.cudnn.benchmark = True
# device detection - cuda or cpu
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

def extract_feats(model):
    """
    :rtype: FloatTensor
    """
    model.eval()
    preprocessing_func = get_preprocessing_pipelines()['test']
    data = preprocessing_func(np.load(args.mouth_patch_path)['data'])  # data: TxHxW
    return model(torch.FloatTensor(data)[None, None, :, :, :].to(device), lengths=[data.shape[0]])


def evaluate(model, dset_loader, criterion):

    model.eval()

    running_loss = 0.
    running_corrects = 0.

    with torch.no_grad():
        for batch_idx, data in enumerate(tqdm(dset_loader)):
            if args.use_boundary:
                input, lengths, labels, boundaries = data
                boundaries = boundaries.to(device)
            else:
                input, lengths, labels = data
                boundaries = None
            # for multiple gpus
            lengths = [lengths[0]]*(len(lengths)//(gpu_num))

            logits = model(input.unsqueeze(1).to(device), lengths=lengths, boundaries=boundaries)
            _, preds = torch.max(F.softmax(logits, dim=1).data, dim=1)
            running_corrects += preds.eq(labels.to(device).view_as(preds)).sum().item()

            loss = criterion(logits, labels.to(device))
            running_loss += loss.item() * input.size(0)

    print(f"{len(dset_loader.dataset)} in total\tCR: {running_corrects/len(dset_loader.dataset)}")
    return running_corrects/len(dset_loader.dataset), running_loss/len(dset_loader.dataset)

def mel_to_wav(mel): ##TODO something's wrong with this (backward propagation problem, torchaudio.transforms uses SGD or whatever)
    invMel_trans = transforms.InverseMelScale(
        sample_rate = 16000,
        n_stft = 1024 // 2 + 1,
    ).to(device)
    waveform = invMel_trans(mel)
    return waveform

def spectrogram_to_wav(spectrogram):
    stft = torch.istft(spectrogram, n_fft = args.n_fft, hop_length = args.hop_length)
    return stft


##TODO need to implement multimodal evaluate function
def multimodal_eval(model, dset_loader, criterion):
    model.eval()
    running_loss = 0.
    running_stoi = 0.

    with torch.no_grad():
        for batch_idx, data in enumerate(tqdm(dset_loader)):
            audio_data,video_data,audio_lengths,video_lengths,audio_raw_mel = data


            # for multiple gpus
            audio_lengths = [audio_lengths[0]]*(len(audio_lengths)//(gpu_num))
            video_lengths = [video_lengths[0]]*(len(video_lengths)//(gpu_num))
            temp = mel_transform(audio_data.detach()) 
            audio_data = audio_data.unsqueeze(1).to(device) 
            video_data = video_data.unsqueeze(1).to(device)
            audio_raw_mel = audio_raw_mel.to(device)
            #print(audio_raw_mel.shape)
            logits = model(audio_data,video_data, audio_lengths,video_lengths)
            # audio_raw_mel.requires_grad = True
            # logits.requires_grad = True
            # label_wav = mel_to_wav(audio_raw_mel)
            # pred_wav = mel_to_wav(logits)
            # plt.figure(figsize=(10, 4))
            # plt.imshow(torch.log(temp.detach().cpu()[0]), aspect='auto', origin='lower')
            # plt.colorbar(format='%+2.0f dB')
            # plt.title('Mel Spectrogram')
            # plt.xlabel('Frames')
            # plt.ylabel('Mel Filterbanks')
            # plt.tight_layout()
            # plt.savefig('./audio_noise_mel.png')
            # plt.figure(figsize=(10, 4))
            # plt.imshow(torch.log(logits.detach().cpu()[0]), aspect='auto', origin='lower')
            # plt.colorbar(format='%+2.0f dB')
            # plt.title('Mel Spectrogram')
            # plt.xlabel('Frames')
            # plt.ylabel('Mel Filterbanks')
            # plt.tight_layout()
            # plt.savefig('./logits.png')
            # plt.figure(figsize=(10, 4))
            # plt.imshow(torch.log(audio_raw_mel.detach().cpu()[0]), aspect='auto', origin='lower')
            # plt.colorbar(format='%+2.0f dB')
            # plt.title('Mel Spectrogram')
            # plt.xlabel('Frames')
            # plt.ylabel('Mel Filterbanks')
            # plt.tight_layout()
            # plt.savefig('./audio_raw_mel.png')
            waveform1 = librosa.feature.inverse.mel_to_audio(logits[0].detach().cpu().numpy(),sr=16000,n_fft=1024 // 2 + 1,hop_length =145, htk=True) # i
            waveform2 = librosa.feature.inverse.mel_to_audio(temp[0].detach().cpu().numpy(),sr=16000,n_fft=1024 // 2 + 1,hop_length =145, htk=True)
            sf.write('logit.wav', waveform1, 16000)
            sf.write('audio_noise.wav', waveform2, 16000)
            #sf.write('test4.wav',audio_data.squeeze().detach().cpu().numpy()[0],16000)
            
            #exit()
            
            #stoi_metric = ShortTimeObjectiveIntelligibility(sr=16000,extended=False)
            loss = criterion(logits,audio_raw_mel) # mse
            running_loss += loss.item() * audio_data.size(0)
            #running_stoi += stoi_metric(pred_wav,label_wav).item() ## TODO need to implement STOI calculation (mask-ground_truth)

    print(f"{len(dset_loader.dataset)} in total\tCR: {running_stoi /len(dset_loader.dataset)}")
    return running_stoi/len(dset_loader.dataset), running_loss/len(dset_loader.dataset)
        

def multimodal_train(model, dset_loader, criterion, epoch, optimizer, logger):
    #return model # test validation
    data_time = AverageMeter()
    batch_time = AverageMeter()

    lr = showLR(optimizer)

    logger.info('-' * 10)
    logger.info(f"Epoch {epoch}/{args.epochs - 1}")
    logger.info(f"Current learning rate: {lr}")

    model.train()
    running_loss = 0.
    running_corrects = 0.
    running_all = 0.

    end = time.time()
    for batch_idx, data in enumerate(dset_loader):
        audio_data,video_data,audio_lengths,video_lengths,audio_raw_mel = data
        # measure data loading time
        data_time.update(time.time() - end)

        #train for multiple gpus
        audio_lengths = [audio_lengths[0]]*(len(audio_lengths)//(gpu_num))
        video_lengths = [video_lengths[0]]*(len(video_lengths)//(gpu_num))
        # --
    
        optimizer.zero_grad()

        # (32, 1, 29, 88, 88)
        # (32, 1, 18560)
        #sf.write('original_audio.wav',audio_data[0],16000) ##TODO need to erase this saving codes .
        audio_data = audio_data.unsqueeze(1).to(device)
        video_data = video_data.unsqueeze(1).to(device)
        audio_raw_mel = audio_raw_mel.to(device)
        #print(audio_raw_mel.shape)
        logits = model(audio_data,video_data, audio_lengths,video_lengths)

        #functionalize this part to check mel-spectrogram every 10 epoch
        # print(logits) #is model working properly?
        # print(logits.shape)
        # torch.save(logits[0],'./masked_audio.pt') # save logit value to play.. ## TODO make function to sample masked & original sound file for testing.
        # plt.figure(figsize=(10, 4))
        # plt.imshow(torch.log(logits.detach().cpu()[0]), aspect='auto', origin='lower')
        # plt.colorbar(format='%+2.0f dB')
        # plt.title('Mel Spectrogram')
        # plt.xlabel('Frames')
        # plt.ylabel('Mel Filterbanks')
        # plt.tight_layout()
        # plt.savefig('./masked_mel.png')
        # exit()
        # #print(logits) #is model working properly?

        #loss_func = mixup_criterion(labels_a, labels_b, lam)
        
        # print("logits", logits.shape)
        # print("audio_raw_mel", audio_raw_mel.shape)
        

        loss = criterion(logits,audio_raw_mel)

        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        # -- compute running performance
        _, predicted = torch.max(F.softmax(logits, dim=1).data, dim=1)
        running_loss += loss.item()*audio_data.size(0)
        running_all += audio_data.size(0)
        # -- log intermediate results
        if batch_idx % args.interval == 0 or (batch_idx == len(dset_loader)-1):
            update_logger_batch(args, logger, dset_loader, batch_idx, running_loss, 0, running_all, batch_time, data_time ) ## TODO STOI implementation?

    return model


def train(model, dset_loader, criterion, epoch, optimizer, logger):
    data_time = AverageMeter()
    batch_time = AverageMeter()

    lr = showLR(optimizer)

    logger.info('-' * 10)
    logger.info(f"Epoch {epoch}/{args.epochs - 1}")
    logger.info(f"Current learning rate: {lr}")

    model.train()
    running_loss = 0.
    running_corrects = 0.
    running_all = 0.

    end = time.time()
    for batch_idx, data in enumerate(dset_loader):
        if args.use_boundary:
            input, lengths, labels, boundaries = data
            boundaries = boundaries.to(device)
        else:
            input, lengths, labels = data
            boundaries = None
        # measure data loading time
        data_time.update(time.time() - end)

        #train for multiple gpus
        lengths = [lengths[0]]*(len(lengths)//(gpu_num))
        # --
        input, labels_a, labels_b, lam = mixup_data(input, labels, args.alpha)
        labels_a, labels_b = labels_a.to(device), labels_b.to(device)

        optimizer.zero_grad()

        # (32, 1, 29, 88, 88)
        # (32, 1, 18560)
        logits = model(input.unsqueeze(1).to(device), lengths=lengths, boundaries=boundaries)
        loss_func = mixup_criterion(labels_a, labels_b, lam) ## TODO : impelemetn loss function
        loss = loss_func(criterion, logits)

        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        # -- compute running performance
        _, predicted = torch.max(F.softmax(logits, dim=1).data, dim=1)
        running_loss += loss.item()*input.size(0)
        running_corrects += lam * predicted.eq(labels_a.view_as(predicted)).sum().item() + (1 - lam) * predicted.eq(labels_b.view_as(predicted)).sum().item()
        running_all += input.size(0)
        # -- log intermediate results
        if batch_idx % args.interval == 0 or (batch_idx == len(dset_loader)-1):
            update_logger_batch( args, logger, dset_loader, batch_idx, running_loss, running_corrects, running_all, batch_time, data_time )

    return model


def get_model_from_json():
    assert args.config_path.endswith('.json') and os.path.isfile(args.config_path), \
        f"'.json' config path does not exist. Path input: {args.config_path}"
    args_loaded = load_json( args.config_path)
    args.backbone_type = args_loaded['backbone_type']
    args.width_mult = args_loaded['width_mult']
    args.relu_type = args_loaded['relu_type']
    args.use_boundary = args_loaded.get("use_boundary", False)

    if args_loaded.get('tcn_num_layers', ''):
        tcn_options = { 'num_layers': args_loaded['tcn_num_layers'],
                        'kernel_size': args_loaded['tcn_kernel_size'],
                        'dropout': args_loaded['tcn_dropout'],
                        'dwpw': args_loaded['tcn_dwpw'],
                        'width_mult': args_loaded['tcn_width_mult'],
                      }
    else:
        tcn_options = {}
    if args_loaded.get('densetcn_block_config', ''):
        densetcn_options = {'block_config': args_loaded['densetcn_block_config'],
                            'growth_rate_set': args_loaded['densetcn_growth_rate_set'],
                            'reduced_size': args_loaded['densetcn_reduced_size'],
                            'kernel_size_set': args_loaded['densetcn_kernel_size_set'],
                            'dilation_size_set': args_loaded['densetcn_dilation_size_set'],
                            'squeeze_excitation': args_loaded['densetcn_se'],
                            'dropout': args_loaded['densetcn_dropout'],
                            }
    else:
        densetcn_options = {}

    if args.modality == "av": ## multi modal lipreading model
        attention_options = {
            'embed_dim' : args.attention_embed_dim,
            'num_heads' : args.attention_num_head,
            'dropout' : args.attention_dropout,
        }
        model = AVLipreading( modality=args.modality,
                        num_classes=args.num_classes,
                        tcn_options=tcn_options,
                        densetcn_options=densetcn_options,
                        attention_options=attention_options,
                        backbone_type=args.backbone_type,
                        relu_type=args.relu_type,
                        width_mult=args.width_mult,
                        use_boundary=args.use_boundary,
                        extract_feats=args.extract_feats)
        calculateNorm2(model)
        return model

    model = Lipreading( modality=args.modality,
                        num_classes=args.num_classes,
                        tcn_options=tcn_options,
                        densetcn_options=densetcn_options,
                        backbone_type=args.backbone_type,
                        relu_type=args.relu_type,
                        width_mult=args.width_mult,
                        use_boundary=args.use_boundary,
                        extract_feats=args.extract_feats)
    calculateNorm2(model)
    return model


def main():

    # -- logging
    save_path = get_save_folder(args)
    print(f"Model and log being saved in: {save_path}")
    logger = get_logger(args, save_path)
    ckpt_saver = CheckpointSaver(save_path)

    ## DATASET LOADER CHECK
    # dset_loaders = get_data_loaders(args) 
    # print("data loaded!,testing...")
    # for batch_idx, data in enumerate(dset_loaders['train']): 
    #     print(data[2])
    #     print(data[0].shape)
    #     print(data[1].shape)
    #     print(data[2])
    #     print(data[4].shape)
    #     print(batch_idx)
    #     break 
    # print("data no problem, exit")
    # exit()
    # -- get model
    model = get_model_from_json()

    # print(gpu_num)
    # -- check CUDA / Multiple device
    if gpu_num>1:
        model = nn.DataParallel(model)
    
    model.to(device)
    ## model size check
    # tot_param = sum(p.numel()for p in model.parameters())
    # print("total_params = ",tot_param)
    # exit()
    
    # -- get dataset iterators
    #dset_loaders = get_data_loaders(args) 
    dset_loaders = unit_test_data_loader(args) # using subset of dataset (currently 48032)
    # -- get loss function
    criterion = nn.MSELoss() if args.modality =="av" else nn.CrossEntropyLoss() 
    # -- get optimizer
    optimizer = get_optimizer(args, optim_policies=model.parameters())
    # -- get learning rate scheduler
    scheduler = CosineScheduler(args.lr, args.epochs)

    if args.model_path:
        assert args.model_path.endswith('.pth') and os.path.isfile(args.model_path), \
            f"'.pth' model path does not exist. Path input: {args.model_path}"
        # resume from checkpoint
        if args.init_epoch > 0:
            model, optimizer, epoch_idx, ckpt_dict = load_model(args.model_path, model, optimizer)
            args.init_epoch = epoch_idx
            ckpt_saver.set_best_from_ckpt(ckpt_dict)
            logger.info(f'Model and states have been successfully loaded from {args.model_path}')
        # init from trained model
        else:
            model = load_model(args.model_path, model, allow_size_mismatch=args.allow_size_mismatch)
            logger.info(f'Model has been successfully loaded from {args.model_path}')
        # feature extraction
        if args.mouth_patch_path:
            save2npz( args.mouth_embedding_out_path, data = extract_feats(model).cpu().detach().numpy())
            return
        # if test-time, performance on test partition and exit. Otherwise, performance on validation and continue (sanity check for reload)
        if args.test:
            if args.modality == "av":
                acc_avg_test, loss_avg_test = multimodal_eval(model, dset_loaders['test'], criterion)    
            else:
                acc_avg_test, loss_avg_test = evaluate(model, dset_loaders['test'], criterion)
            logger.info(f"Test-time performance on partition {'test'}: Loss: {loss_avg_test:.4f}\tAcc:{acc_avg_test:.4f}")
            return

    # -- fix learning rate after loading the ckeckpoint (latency)
    if args.model_path and args.init_epoch > 0:
        scheduler.adjust_lr(optimizer, args.init_epoch-1)

    epoch = args.init_epoch

    while epoch < args.epochs:
        if args.modality == "av":
            model = multimodal_train(model,dset_loaders['train'],criterion,epoch,optimizer,logger) # avlipreading
        else:
            model = train(model, dset_loaders['train'], criterion, epoch, optimizer, logger) # optimize?
        
        if args.modality =="av":
            acc_avg_val, loss_avg_val = multimodal_eval(model,dset_loaders['val'],criterion)
        else:
            acc_avg_val, loss_avg_val = evaluate(model, dset_loaders['val'], criterion)
        logger.info(f"{'val'} Epoch:\t{epoch:2}\tLoss val: {loss_avg_val:.4f}\tAcc val:{acc_avg_val:.4f}, LR: {showLR(optimizer)}")
        # -- save checkpoint
        save_dict = {
            'epoch_idx': epoch + 1,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict()
        }
        ckpt_saver.save(save_dict, acc_avg_val)
        scheduler.adjust_lr(optimizer, epoch)
        epoch += 1

    # -- evaluate best-performing epoch on test partition
    best_fp = os.path.join(ckpt_saver.save_dir, ckpt_saver.best_fn)
    _ = load_model(best_fp, model)
    if args.modality == "av":
        acc_avg_test, loss_avg_test = multimodal_eval(model, dset_loaders['test'], criterion)
    else:
        acc_avg_test, loss_avg_test = evaluate(model, dset_loaders['test'], criterion)
    logger.info(f"Test time performance of best epoch: {acc_avg_test} (loss: {loss_avg_test})")

if __name__ == '__main__':
    main()
