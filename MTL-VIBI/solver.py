import os
import sys
import time
import math
import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable
from torch.optim import lr_scheduler
# from torch.utils.data import DataLoader
# from torchvision import transforms
from tensorboardX import SummaryWriter
from utils import cuda, Weight_EMA_Update, label2binary, save_batch, index_transfer, timeSince, UnknownDatasetError, \
    idxtobool
from return_data import return_data
from pathlib import Path
from sklearn.metrics import f1_score, precision_score, recall_score, roc_auc_score


class Solver(object):

    def __init__(self, args):

        self.args = args
        self.dataset = args.dataset
        self.epoch = args.epoch
        self.save_image = args.save_image
        self.save_checkpoint = args.save_checkpoint
#         self.batch_size = args.batch_size
        self.batch_size = 10
        self.lr = args.lr  # learning rate
        self.beta = args.beta
        self.cuda = args.cuda
        self.device = torch.device("cuda" if args.cuda else "cpu")
        self.num_avg = args.num_avg
        self.global_iter = 0
        self.global_epoch = 0
        self.env_name = os.path.splitext(args.checkpoint_name)[0] if args.env_name is 'main' else args.env_name
        self.start = time.time()
        self.args.word_idx =None

        print(os.getcwd())

        # Dataset
        self.args.root = os.path.join(self.args.dataset, self.args.data_dir)
        self.args.load_pred = True
        self.data_loader = return_data(args=self.args)

        train_loader = self.data_loader['train']

        if 'mnist' in self.dataset:

            for batch_idx, (x, target_c, target_s, _) in enumerate(train_loader):
                self.xsize = x.size()
                print(self.xsize)
                if batch_idx == 2:
                    break

            self.d = torch.tensor(self.xsize[1:]).prod()  # C * W * H
            self.x_type = self.data_loader['x_type']
            self.y_type = self.data_loader['yc_type']

            sys.path.append("./" + self.dataset)

            self.original_ncol = 224
            self.original_nrow = 224
            self.args.chunk_size = self.args.chunk_size if self.args.chunk_size > 0 else 2
            self.chunk_size = self.args.chunk_size
            assert np.remainder(self.original_nrow, self.chunk_size) == 0
            self.filter_size = (self.chunk_size, self.chunk_size)
            self.idx_list = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]

            # load black box model
            from mnist.original import Net
            self.black_box = Net().to(self.device)   # 训练好的模型

        else:
            raise UnknownDatasetError()

        # Black box
        self.args.model_dir = args.dataset + '/models'
        model_name = Path(self.args.model_dir).joinpath(self.args.model_name)
        self.black_box.load_state_dict(torch.load(str(model_name), map_location='cpu'))

        if self.cuda:
            self.black_box.cuda()

        if torch.cuda.device_count() is 0:
            self.black_box.eval()
        else:
            self.black_box.eval()

        from mnist.explainer import Explainer, prior
        self.prior = prior

        # Network
        self.net = cuda(Explainer(args=self.args), self.args.cuda)
        self.net.weight_init()
        self.net_ema = Weight_EMA_Update(cuda(Explainer(args=self.args), self.args.cuda), self.net.state_dict(),
                                         decay=0.999)

        # Optimizer
        self.optim = optim.Adam(self.net.parameters(), lr=self.lr, betas=(0.5, 0.999))
        self.scheduler = lr_scheduler.ExponentialLR(self.optim, gamma=0.97)

        # Checkpoint
        self.checkpoint_dir = Path(args.dataset).joinpath(args.checkpoint_dir, args.env_name)
        if not self.checkpoint_dir.exists(): self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = Path(args.dataset).joinpath(args.checkpoint_dir, 'sample')
        if not self.image_dir.exists(): self.image_dir.mkdir(parents=True, exist_ok=True)
        self.load_checkpoint = args.load_checkpoint
        if self.load_checkpoint != '': self.load_checkpoints(self.load_checkpoint)
        self.checkpoint_name = args.checkpoint_name

        if self.save_checkpoint:
            # History
            self.history = dict()
            self.history['info_loss'] = 0.
            self.history['class_loss'] = 0.
            self.history['total_loss'] = 0.
            self.history['epoch'] = 0
            self.history['iter'] = 0

            self.history['avg_acc'] = 0.
            self.history['avg_acc_fixed'] = 0.
            self.history['avg_precision_macro'] = 0.
            #            self.history['avg_precision_micro'] = 0.
            self.history['avg_precision_fixed_macro'] = 0.
            #            self.history['avg_precision_fixed_micro'] = 0.
            self.history['avg_recall_macro'] = 0.
            #            self.history['avg_recall_micro'] = 0.
            self.history['avg_recall_fixed_macro'] = 0.
            #            self.history['avg_recall_fixed_micro'] = 0.
            self.history['avg_f1_macro'] = 0.
            self.history['avg_f1_micro'] = 0.
            self.history['avg_f1_fixed_macro'] = 0.
            self.history['avg_f1_fixed_micro'] = 0.

            self.history['avg_vmi'] = 0.
            self.history['avg_vmi_fixed'] = 0.

        # Tensorboard
        self.tensorboard = args.tensorboard
        if self.tensorboard:
            self.summary_dir = Path(args.dataset).joinpath(args.summary_dir, self.env_name)
            if not self.summary_dir.exists(): self.summary_dir.mkdir(parents=True, exist_ok=True)
            self.tf = SummaryWriter(log_dir=str(self.summary_dir))
            self.tf.add_text(tag='argument', text_string=str(args), global_step=self.global_epoch)

    def set_mode(self, mode='train'):
        if mode == 'train':
            self.net.train()
            self.net_ema.model.train()

        elif mode == 'eval':
            self.net.eval()
            self.net_ema.model.eval()

        else:
            raise ('mode error. It should be either train or eval')
    
    def save_checkpoints(self, filename='best_acc.tar'):
        model_states = {
            'net': self.net.state_dict(),
            'net_ema': self.net_ema.model.state_dict(),
        }
        optim_states = {
            'optim': self.optim.state_dict(),
        }
        states = {
            'iter': self.global_iter,
            'epoch': self.global_epoch,
            'history': self.history,
            'args': self.args,
            'model_states': model_states,
            'optim_states': optim_states,
        }

        file_path = self.checkpoint_dir.joinpath(filename)
        # torch.save(states, file_path.open('wb+'))
        torch.save(states, open(str(file_path), 'wb+'))

        print("=> saved checkpoint '{}' (iter {})".format(file_path, self.global_iter))

    def load_checkpoints(self, filename='best_acc.tar'):

        file_path = self.checkpoint_dir.joinpath(filename)
        if file_path.is_file():
            print("=> loading checkpoint '{}'".format(file_path))
            # checkpoint = torch.load(file_path.open('rb'))
            checkpoint = torch.load(open(str(file_path), 'rb'))
            self.global_epoch = checkpoint['epoch']
            self.global_iter = checkpoint['iter']
            self.history = checkpoint['history']

            self.net.load_state_dict(checkpoint['model_states']['net'])
            self.net_ema.model.load_state_dict(checkpoint['model_states']['net_ema'])

            print("=> loaded checkpoint '{} (iter {})'".format(
                file_path, self.global_iter))

        else:
            print("=> no checkpoint found at '{}'".format(file_path))
            
    
    
    def train(self, test=False):

        self.set_mode('train')

        self.class_criterion = nn.CrossEntropyLoss(reduction='sum')
        self.info_criterion = nn.KLDivLoss(reduction='sum')

        start = time.time()
        for e in range(self.epoch):

            self.global_epoch += 1

            for idx, batch in enumerate(self.data_loader['train']):

                if 'mnist' in self.dataset:

                    x_raw = batch[0]
                    y_raw = batch[1]
                    y_s_raw = batch[2]

                else:

                    raise UnknownDatasetError()

                self.global_iter += 1

                x = Variable(cuda(x_raw, self.args.cuda)).type(self.x_type)
                y = Variable(cuda(y_raw, self.args.cuda)).type(self.y_type)

                # raise ValueError('num_sample should be a positive integer')
                logit, log_p_i, Z_hat, logit_fixed = self.net(x) # net是解释器网络

                # prior distribution
                p_i_prior = cuda(self.prior(var_size=log_p_i.size()), self.args.cuda)

                # define loss
                y_class = y if len(y.size()) == 1 else torch.argmax(y, dim=-1)
                # y_class = label2binary(y_class, classes=range(logit.size(-1)))

                class_loss = self.class_criterion(logit, y_class).div(math.log(2)) / self.batch_size
                info_loss = self.args.K * self.info_criterion(log_p_i, p_i_prior) / self.batch_size
                total_loss = class_loss + self.beta * info_loss

                izy_bound = math.log(10, 2) - class_loss
                izx_bound = info_loss

                self.optim.zero_grad()
                total_loss.backward()
                self.optim.step()
                self.net_ema.update(self.net.state_dict()) # 更新解释器网络参数

                if self.global_iter % 1000 == 0:

                    prediction = torch.argmax(logit, dim=-1)
                    accuracy = torch.eq(prediction, y_class).float().mean()
                    prediction_fixed = torch.argmax(logit_fixed, dim=-1)
                    accuracy_fixed = torch.eq(prediction_fixed, y_class).float().mean()
                    
                    y_class, prediction, prediction_fixed = y_class.cpu(), prediction.cpu(), prediction_fixed.cpu()
                    
                    precision_macro = precision_score(y_class, prediction, average='macro')
                    precision_micro = precision_score(y_class, prediction, average='micro')
                    precision_fixed_macro = precision_score(y_class, prediction_fixed, average='macro')
                    precision_fixed_micro = precision_score(y_class, prediction_fixed, average='micro')
                    recall_macro = recall_score(y_class, prediction, average='macro')
                    recall_micro = recall_score(y_class, prediction, average='micro')
                    recall_fixed_macro = recall_score(y_class, prediction_fixed, average='macro')
                    recall_fixed_micro = recall_score(y_class, prediction_fixed, average='micro')
                    f1_macro = f1_score(y_class, prediction, average='macro')
                    f1_micro = f1_score(y_class, prediction, average='micro')
                    f1_fixed_macro = f1_score(y_class, prediction_fixed, average='macro')
                    f1_fixed_micro = f1_score(y_class, prediction_fixed, average='micro')

                    # Post-hoc Accuracy (zero-padded accuracy)
                    output_original,_ = self.black_box(x)
                    vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                                  tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                                  tensor2=logit.type(torch.FloatTensor) - torch.logsumexp(
                                                      output_original, dim=0).unsqueeze(0).expand(logit.size()).type(
                                                      torch.FloatTensor) + torch.log(
                                                      torch.tensor(output_original.size(0)).type(torch.FloatTensor)),
                                                  out=None), dim=-1)
                    vmi_fidel = vmi.mean()

                    vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                                  tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                                  tensor2=logit_fixed.type(torch.FloatTensor) - torch.logsumexp(
                                                      output_original, dim=0).unsqueeze(0).expand(
                                                      logit_fixed.size()).type(torch.FloatTensor) + torch.log(
                                                      torch.tensor(output_original.size(0)).type(torch.FloatTensor)),
                                                  out=None), dim=-1)
                    vmi_fidel_fixed = vmi.mean()

                    if self.num_avg != 0:
                        avg_soft_logit, avg_log_p_i, _, avg_soft_logit_fixed = self.net(x, self.num_avg)
                        avg_prediction = avg_soft_logit.max(1)[1]
                        
                        avg_accuracy = torch.eq(cuda(avg_prediction, self.args.cuda), cuda(y_class, self.args.cuda)).float().mean()
                        avg_prediction_fixed = avg_soft_logit_fixed.max(1)[1]
                        avg_accuracy_fixed = torch.eq(avg_prediction_fixed, cuda(y_class, self.args.cuda)).float().mean()
                        
                        y_class, avg_prediction, prediction_fixed, avg_prediction_fixed = y_class.cpu(), avg_prediction.cpu(), prediction_fixed.cpu(), avg_prediction_fixed.cpu()
                    
                        avg_precision_macro = precision_score(y_class, avg_prediction, average='macro')
                        #                    avg_precision_micro = precision_score(y_class, avg_prediction, average = 'micro')
                        avg_precision_fixed_macro = precision_score(y_class, avg_prediction_fixed, average='macro')
                        #                    avg_precision_fixed_micro = precision_score(y_class, avg_prediction_fixed, average = 'micro')
                        avg_recall_macro = recall_score(y_class, avg_prediction, average='macro')
                        #                    avg_recall_micro = recall_score(y_class, avg_prediction, average = 'micro')
                        avg_recall_fixed_macro = recall_score(y_class, avg_prediction_fixed, average='macro')
                        #                    avg_recall_fixed_micro = recall_score(y_class, avg_prediction_fixed, average = 'micro')
                        avg_f1_macro = f1_score(y_class, avg_prediction, average='macro')
                        avg_f1_micro = f1_score(y_class, avg_prediction, average='micro')
                        avg_f1_fixed_macro = f1_score(y_class, avg_prediction_fixed, average='macro')
                        avg_f1_fixed_micro = f1_score(y_class, avg_prediction_fixed, average='micro')

                        ## Variational Mutual Information            
                        vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                                      tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                                      tensor2=avg_soft_logit.type(torch.FloatTensor) - torch.logsumexp(
                                                          output_original, dim=0).unsqueeze(0).expand(
                                                          avg_soft_logit.size()).type(torch.FloatTensor) + torch.log(
                                                          torch.tensor(output_original.size(0)).type(
                                                              torch.FloatTensor)),
                                                      out=None), dim=-1)
                        avg_vmi_fidel = vmi.mean()

                        vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                                      tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                                      tensor2=avg_soft_logit_fixed.type(
                                                          torch.FloatTensor) - torch.logsumexp(output_original,
                                                                                               dim=0).unsqueeze(
                                                          0).expand(avg_soft_logit_fixed.size()).type(
                                                          torch.FloatTensor) + torch.log(
                                                          torch.tensor(output_original.size(0)).type(
                                                              torch.FloatTensor)),
                                                      out=None), dim=-1)
                        avg_vmi_fidel_fixed = vmi.mean()

                    else:
                        avg_accuracy = accuracy
                        avg_accuracy_fixed = accuracy_fixed
                        #                    avg_precision_macro = precision_macro
                        avg_precision_micro = precision_micro
                        #                    avg_precision_fixed_macro = precision_fixed_macro
                        avg_precision_fixed_micro = precision_fixed_micro
                        #                    avg_recall_macro = recall_macro
                        avg_recall_micro = recall_micro
                        #                    avg_recall_fixed_macro = recall_fixed_macro
                        avg_recall_fixed_micro = recall_fixed_micro
                        avg_f1_macro = f1_macro
                        avg_f1_micro = f1_micro
                        avg_f1_fixed_macro = f1_fixed_macro
                        avg_f1_fixed_micro = f1_fixed_micro

                        avg_vmi_fidel = vmi_fidel
                        avg_vmi_fidel_fixed = vmi_fidel_fixed

                    print('\n\n[TRAINING RESULT]\n')
                    print('epoch {} Time since {}'.format(self.global_epoch, timeSince(self.start)), end="\n")
                    print('global iter {}'.format(self.global_iter), end="\n")
                    print('i:{} IZY:{:.2f} IZX:{:.2f}'
                          .format(idx + 1, izy_bound.item(), izx_bound.item()), end='\n')
                    print('acc:{:.4f} avg_acc:{:.4f}'
                          .format(accuracy.item(), avg_accuracy.item()), end='\n')
                    print('acc_fixed:{:.4f} avg_acc_fixed:{:.4f}'
                          .format(accuracy_fixed.item(), avg_accuracy_fixed.item()), end='\n')
                    print('vmi:{:.4f} avg_vmi:{:.4f}'
                          .format(vmi_fidel.item(), avg_vmi_fidel.item()), end='\n')
                    print('vmi_fixed:{:.4f} avg_vmi_fixed:{:.4f}'
                          .format(vmi_fidel_fixed.item(), avg_vmi_fidel_fixed.item()), end='\n')

                    if self.tensorboard:
                        self.tf.add_scalars(main_tag='performance/accuracy',
                                            tag_scalar_dict={
                                                'train_one-shot': accuracy.item(),
                                                'train_multi-shot': avg_accuracy.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/accuracy_fixed',
                                            tag_scalar_dict={
                                                'train_one-shot': accuracy_fixed.item(),
                                                'train_multi-shot': avg_accuracy_fixed.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/vmi',
                                            tag_scalar_dict={
                                                'train_one-shot': vmi_fidel.item(),
                                                'train_multi-shot': avg_vmi_fidel.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/vmi_fixed',
                                            tag_scalar_dict={
                                                'train_one-shot': vmi_fidel_fixed.item(),
                                                'train_multi-shot': avg_vmi_fidel_fixed.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/precision_macro',
                                            tag_scalar_dict={
                                                'train_one-shot': precision_macro.item(),
                                                'train_multi-shot': avg_precision_macro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/precision_fixed_macro',
                                            tag_scalar_dict={
                                                'train_one-shot': precision_fixed_macro.item(),
                                                'train_multi-shot': avg_precision_fixed_macro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/recall_macro',
                                            tag_scalar_dict={
                                                'train_one-shot': recall_macro.item(),
                                                'train_multi-shot': avg_recall_macro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/recall_fixed_macro',
                                            tag_scalar_dict={
                                                'train_one-shot': recall_fixed_macro.item(),
                                                'train_multi-shot': avg_recall_fixed_macro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/f1_macro',
                                            tag_scalar_dict={
                                                'train_one-shot': f1_macro.item(),
                                                'train_multi-shot': avg_f1_macro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/f1_micro',
                                            tag_scalar_dict={
                                                'train_one-shot': f1_micro.item(),
                                                'train_multi-shot': avg_f1_micro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/f1_fixed_macro',
                                            tag_scalar_dict={
                                                'train_one-shot': f1_fixed_macro.item(),
                                                'train_multi-shot': avg_f1_fixed_macro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/f1_fixed_micro',
                                            tag_scalar_dict={
                                                'train_one-shot': f1_fixed_micro.item(),
                                                'train_multi-shot': avg_f1_fixed_micro.item()
                                            },
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='performance/cost',
                                            tag_scalar_dict={
                                                'train_one-shot_class': class_loss.item(),
                                                'train_one-shot_info': info_loss.item(),
                                                'train_one-shot_total': total_loss.item()},
                                            global_step=self.global_iter)
                        self.tf.add_scalars(main_tag='mutual_information/train',
                                            tag_scalar_dict={
                                                'I(Z;Y)': izy_bound.item(),
                                                'I(Z;X)': izx_bound.item()},
                                            global_step=self.global_iter)

            self.val(test=test)

            print("epoch:{}".format(e + 1))
            print('Time spent is {}'.format(time.time() - start))

        print(" [*] Training Finished!")

    def val(self, test=False):
        print('test', test)

        self.set_mode('eval')
        # self.class_criterion_val = nn.CrossEntropyLoss()#size_average = False)
        # self.info_criterion_val = nn.KLDivLoss()#size_average = False)
        self.class_criterion_val = nn.CrossEntropyLoss(reduction='sum')
        self.info_criterion_val = nn.KLDivLoss(reduction='sum')
        class_loss = 0
        info_loss = 0
        total_loss = 0
        izy_bound = 0
        izx_bound = 0

        vmi_fidel_sum = 0
        vmi_fidel_fixed_sum = 0
        avg_vmi_fidel_sum = 0
        avg_vmi_fidel_fixed_sum = 0

        correct = 0
        correct_fixed = 0
        precision_macro = 0
        recall_macro = 0
        f1_macro = 0
        f1_micro = 0
        precision_fixed_macro = 0
        recall_fixed_macro = 0
        f1_fixed_macro = 0
        f1_fixed_micro = 0
        avg_correct = 0
        avg_correct_fixed = 0
        avg_precision_macro = 0
        avg_recall_macro = 0
        avg_f1_macro = 0
        avg_f1_micro = 0
        avg_precision_fixed_macro = 0
        avg_recall_fixed_macro = 0
        avg_f1_fixed_macro = 0
        avg_f1_fixed_micro = 0
        # avg_f1_fixed_weighted = 0
        total_num = 0
        total_num_ind = 0

        with torch.no_grad():
            data_type = 'test' if test else 'valid'
            for idx, batch in enumerate(self.data_loader[data_type]):

                if 'mnist' in self.dataset:

                    x_raw, y_ori = batch[0], batch[1]
                    x = Variable(cuda(x_raw, self.args.cuda)).type(self.x_type)
                    pred_c, pred_s = self.black_box(x)
                    y = torch.argmax(pred_c, dim=-1)

                # model fit
                # x = Variable(cuda(x_raw, self.args.cuda)).type(self.x_type)
                # y = Variable(cuda(y_raw, self.args.cuda)).type(self.y_type)
                y_ori = Variable(cuda(y_ori, self.args.cuda)).type(self.y_type)
                
                # 
                logit, log_p_i, Z_hat, logit_fixed = self.net_ema.model(x)
                # logit, log_p_i, Z_hat, logit_fixed = self.net(x)

                # prior distribution
                p_i_prior = cuda(self.prior(var_size=log_p_i.size()), self.args.cuda)

                # define loss
                y_class = y if len(y.size()) == 1 else torch.argmax(y, dim=-1)
                #            y_binary = label2binary(y_class, classes = range(logit.size(-1)))

                class_loss += self.class_criterion_val(logit, y_class).div(math.log(2)) / self.batch_size
                info_loss += self.args.K * self.info_criterion_val(log_p_i, p_i_prior) / self.batch_size
                total_loss += class_loss + self.beta * info_loss
                total_num += 1
                total_num_ind += y_class.size(0)

                prediction = F.softmax(logit, dim=1).max(1)[1]
                correct += torch.eq(prediction, y_class).float().sum()

                prediction_fixed = F.softmax(logit_fixed, dim=1).max(1)[1]
                correct_fixed += torch.eq(prediction_fixed, y_class).float().sum()
                y_class, prediction, prediction_fixed = y_class.cpu(), prediction.cpu(), prediction_fixed.cpu()
                precision_macro += precision_score(y_class, prediction, average='macro')
                precision_fixed_macro += precision_score(y_class, prediction_fixed, average='macro')
                recall_macro += recall_score(y_class, prediction, average='macro')
                recall_fixed_macro += recall_score(y_class, prediction_fixed, average='macro')
                #                recall_weighted += recall_score(y_class, prediction, average = 'weighted')
                f1_macro += f1_score(y_class, prediction, average='macro')
                f1_micro += f1_score(y_class, prediction, average='micro')
                f1_fixed_macro += f1_score(y_class, prediction_fixed, average='macro')
                f1_fixed_micro += f1_score(y_class, prediction_fixed, average='micro')

                # selected chunk index
                _, index_chunk = log_p_i.unsqueeze(1).topk(self.args.K, dim=-1)

                if self.chunk_size is not 1:
                    index_chunk = index_transfer(dataset=self.dataset,
                                                 idx=index_chunk,
                                                 filter_size=self.filter_size,
                                                 original_nrow=self.original_nrow,
                                                 original_ncol=self.original_ncol,
                                                 is_cuda=self.cuda).output

                # Post-hoc Accuracy (zero-padded accuracy)
                output_original, output_original2 = self.black_box(x)

                # Variational Mutual Information
                vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                              tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                              tensor2=logit.type(torch.FloatTensor) - torch.logsumexp(output_original,
                                                                                                      dim=0).unsqueeze(
                                                  0).expand(logit.size()).type(torch.FloatTensor) + torch.log(
                                                  torch.tensor(output_original.size(0)).type(torch.FloatTensor)),
                                              out=None), dim=-1)
                vmi_fidel_sum += vmi.sum()

                vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                              tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                              tensor2=logit_fixed.type(torch.FloatTensor) - torch.logsumexp(
                                                  output_original, dim=0).unsqueeze(0).expand(logit_fixed.size()).type(
                                                  torch.FloatTensor) + torch.log(
                                                  torch.tensor(output_original.size(0)).type(torch.FloatTensor)),
                                              out=None), dim=-1)
                vmi_fidel_fixed_sum += vmi.sum()

                if self.num_avg != 0:
                    avg_soft_logit, avg_log_p_i, _, avg_soft_logit_fixed = self.net_ema.model(x, self.num_avg)
                    # avg_soft_logit, _, _, avg_soft_logit_fixed = self.net(x,self.num_avg)
                    avg_prediction = avg_soft_logit.max(1)[1]
                    
                    avg_prediction, y_class = avg_prediction.cpu(), y_class.cpu()
                    avg_soft_logit, avg_log_p_i, avg_soft_logit_fixed = avg_soft_logit.cpu(), avg_log_p_i.cpu(), avg_soft_logit_fixed.cpu()
                    
                    avg_correct += torch.eq(avg_prediction, y_class).float().sum()
                    avg_prediction_fixed = avg_soft_logit_fixed.max(1)[1]
                    avg_correct_fixed += torch.eq(avg_prediction_fixed, y_class).float().sum()
                    avg_precision_macro += precision_score(y_class, avg_prediction, average='macro')
                    avg_recall_macro += recall_score(y_class, avg_prediction, average='macro')
                    avg_f1_macro += f1_score(y_class, avg_prediction, average='macro')
                    avg_f1_micro += f1_score(y_class, avg_prediction, average='micro')
                    avg_precision_fixed_macro += precision_score(y_class, avg_prediction_fixed, average='macro')
                    avg_recall_fixed_macro += recall_score(y_class, avg_prediction_fixed, average='macro')
                    avg_f1_fixed_macro += f1_score(y_class, avg_prediction_fixed, average='macro')
                    avg_f1_fixed_micro += f1_score(y_class, avg_prediction_fixed, average='micro')

                    # Variational Mutual Information
                    vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                                  tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                                  tensor2=avg_soft_logit.type(torch.FloatTensor) - torch.logsumexp(
                                                      output_original, dim=0).unsqueeze(0).expand(
                                                      avg_soft_logit.size()).type(torch.FloatTensor) + torch.log(
                                                      torch.tensor(output_original.size(0)).type(torch.FloatTensor)),
                                                  out=None), dim=-1)
                    avg_vmi_fidel_sum += vmi.sum()

                    vmi = torch.sum(torch.addcmul(torch.zeros(1), value=1,
                                                  tensor1=torch.exp(output_original).type(torch.FloatTensor),
                                                  tensor2=avg_soft_logit_fixed.type(
                                                      torch.FloatTensor) - torch.logsumexp(output_original,
                                                                                           dim=0).unsqueeze(0).expand(
                                                      avg_soft_logit_fixed.size()).type(torch.FloatTensor) + torch.log(
                                                      torch.tensor(output_original.size(0)).type(torch.FloatTensor)),
                                                  out=None), dim=-1)
                    avg_vmi_fidel_fixed_sum += vmi.sum()

                else:
                    avg_correct = correct
                    avg_correct_fixed = correct_fixed
                    avg_precision_macro = precision_macro
                    avg_recall_macro = recall_macro
                    avg_f1_macro = f1_macro
                    avg_f1_micro = f1_micro
                    # avg_f1_weighted = f1_weighted

                    avg_precision_fixed_macro = precision_fixed_macro
                    avg_recall_fixed_macro = recall_fixed_macro
                    avg_f1_fixed_macro = f1_fixed_macro
                    avg_f1_fixed_micro = f1_fixed_micro
                    # avg_f1_fixed_weighted = f1_fixed_weighted

                    avg_vmi_fidel_sum = vmi_fidel_sum
                    avg_vmi_fidel_fixed_sum = vmi_fidel_fixed_sum

                # %% save image #
                if self.save_image and (self.global_epoch % 10 == 0 and self.global_epoch > 75):
                    # print("SAVED!!!!")
                    if idx in self.idx_list:  # (idx == 0 or idx == 200):

                        # filename
                        img_name, _ = os.path.splitext(self.checkpoint_name)
                        img_name = 'figure_' + img_name + '_' + str(self.global_epoch) + "_" + str(idx) + '.png'
                        img_name = Path(self.image_dir).joinpath(img_name)

                        save_batch(dataset=self.dataset,
                                   batch=x,
                                   label=y_ori, label_pred=y_class, label_approx=prediction,
                                   index=index_chunk,
                                   filename=img_name,
                                   is_cuda=self.cuda,
                                   word_idx=self.args.word_idx).output  ##

            vmi_fidel = vmi_fidel_sum / total_num_ind
            vmi_fidel_fixed = vmi_fidel_fixed_sum / total_num_ind
            avg_vmi_fidel = avg_vmi_fidel_sum / total_num_ind
            avg_vmi_fidel_fixed = avg_vmi_fidel_fixed_sum / total_num_ind

            ## Approximation Fidelity (prediction performance)            
            accuracy = correct / total_num_ind
            avg_accuracy = avg_correct / total_num_ind
            accuracy_fixed = correct_fixed / total_num_ind
            avg_accuracy_fixed = avg_correct_fixed / total_num_ind
            precision_macro = precision_macro / total_num
            recall_macro = recall_macro / total_num
            f1_macro = f1_macro / total_num
            f1_micro = f1_micro / total_num

            precision_fixed_macro = precision_fixed_macro / total_num
            recall_fixed_macro = recall_fixed_macro / total_num
            f1_fixed_macro = f1_fixed_macro / total_num
            f1_fixed_micro = f1_fixed_micro / total_num

            avg_precision_macro = avg_precision_macro / total_num
            avg_recall_macro = avg_recall_macro / total_num
            avg_f1_macro = avg_f1_macro / total_num
            avg_f1_micro = avg_f1_micro / total_num
            # avg_f1_weighted = avg_f1_weighted/total_num

            avg_precision_fixed_macro = avg_precision_fixed_macro / total_num
            avg_recall_fixed_macro = avg_recall_fixed_macro / total_num
            avg_f1_fixed_macro = avg_f1_fixed_macro / total_num
            avg_f1_fixed_micro = avg_f1_fixed_micro / total_num
            # avg_f1_fixed_weighted = avg_f1_fixed_weighted/total_num

            class_loss /= total_num
            info_loss /= total_num
            total_loss /= total_num
            izy_bound = math.log(10, 2) - class_loss
            izx_bound = info_loss
            
            if data_type = 'test':
                print('\n\n[Test RESULT]\n')
            else:
                print('\n\n[VAL RESULT]\n')
            print('epoch {}'.format(self.global_epoch), end="\n")
            print('global iter {}'.format(self.global_iter), end="\n")
            print('IZY:{:.2f} IZX:{:.2f}'
                  .format(izy_bound.item(), izx_bound.item()), end='\n')
            print('acc:{:.4f} avg_acc:{:.4f}'
                  .format(accuracy.item(), avg_accuracy.item()), end='\n')
            print('acc_fixed:{:.4f} avg_acc_fixed:{:.4f}'
                  .format(accuracy_fixed.item(), avg_accuracy_fixed.item()), end='\n')
            print('vmi:{:.4f} avg_vmi:{:.4f}'
                  .format(vmi_fidel.item(), avg_vmi_fidel.item()), end='\n')
            print('vmi_fixed:{:.4f} avg_vmi_fixed:{:.4f}'
                  .format(vmi_fidel_fixed.item(), avg_vmi_fidel_fixed.item()), end='\n')
            print()

            if self.save_checkpoint and (self.history['avg_acc'] < avg_accuracy.item()):

                self.history['class_loss'] = class_loss.item()
                self.history['info_loss'] = info_loss.item()
                self.history['total_loss'] = total_loss.item()
                self.history['epoch'] = self.global_epoch
                self.history['iter'] = self.global_iter

                self.history['avg_acc'] = avg_accuracy.item()
                self.history['avg_acc_fixed'] = avg_accuracy_fixed.item()
                #            self.history['avg_auc_macro'] = avg_auc_macro.item()
                #            self.history['avg_auc_micro'] = avg_auc_micro.item()
                #            self.history['avg_auc_weighted'] = avg_auc_weighted.item()
                self.history['avg_precision_macro'] = avg_precision_macro
                #                self.history['avg_precision_micro'] = avg_precision_micro
                # self.history['avg_precision_weighted'] = avg_precision_weighted
                self.history['avg_recall_macro'] = avg_recall_macro
                #               self.history['avg_recall_micro'] = avg_recall_micro
                # self.history['avg_recall_weighted'] = avg_recall_weighted
                self.history['avg_f1_macro'] = avg_f1_macro
                self.history['avg_f1_micro'] = avg_f1_micro
                # self.history['avg_f1_weighted'] = avg_f1_weighted

                self.history['avg_precision_fixed_macro'] = avg_precision_fixed_macro
                #                self.history['avg_precision_fixed_micro'] = avg_precision_fixed_micro
                # self.history['avg_precision_fixed_weighted'] = avg_precision_fixed_weighted
                self.history['avg_recall_fixed_macro'] = avg_recall_fixed_macro
                #                self.history['avg_recall_fixed_micro'] = avg_recall_fixed_micro
                # self.history['avg_recall_fixed_weighted'] = avg_recall_fixed_weighted
                self.history['avg_f1_fixed_macro'] = avg_f1_fixed_macro
                #                self.history['avg_f1_fixed_micro'] = avg_f1_fixed_micro
                # self.history['avg_f1_fixed_weighted'] = avg_f1_fixed_weighted
                self.history['avg_vmi'] = avg_vmi_fidel.item()
                self.history['avg_vmi_fixed'] = avg_vmi_fidel_fixed.item()

                # if save_checkpoint : self.save_checkpoint('best_acc.tar')
                if not test: 
                    self.save_checkpoints(self.checkpoint_name)

            if self.tensorboard:
                self.tf.add_scalars(main_tag='performance/accuracy',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': accuracy.item(),
                                        data_type + '_multi-shot': avg_accuracy.item()
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/accuracy_fixed',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': accuracy_fixed.item(),
                                        data_type + '_multi-shot': avg_accuracy_fixed.item()
                                    },
                                    global_step=self.global_iter)

                self.tf.add_scalars(main_tag='performance/vmi',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': vmi_fidel.item(),
                                        data_type + '_multi-shot': avg_vmi_fidel.item()
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/vmi_fixed',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': vmi_fidel_fixed.item(),
                                        data_type + '_multi-shot': avg_vmi_fidel_fixed.item()
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/precision_macro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': precision_macro,
                                        data_type + '_multi-shot': avg_precision_macro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/recall_macro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': recall_macro,
                                        data_type + '_multi-shot': avg_recall_macro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/f1_macro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': f1_macro,
                                        data_type + '_multi-shot': avg_f1_macro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/f1_micro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': f1_micro,
                                        data_type + '_multi-shot': avg_f1_micro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/precision_fixed_macro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': precision_fixed_macro,
                                        data_type + '_multi-shot': avg_precision_fixed_macro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/recall_fixed_macro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': recall_fixed_macro,
                                        data_type + '_multi-shot': avg_recall_fixed_macro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/f1_fixed_macro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': f1_fixed_macro,
                                        data_type + '_multi-shot': avg_f1_fixed_macro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/f1_fixed_micro',
                                    tag_scalar_dict={
                                        data_type + '_one-shot': f1_fixed_micro,
                                        data_type + '_multi-shot': avg_f1_fixed_micro
                                    },
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='performance/cost',
                                    tag_scalar_dict={
                                        data_type + '_one-shot_class': class_loss.item(),
                                        data_type + '_one-shot_info': info_loss.item(),
                                        data_type + '_one-shot_total': total_loss.item()},
                                    global_step=self.global_iter)
                self.tf.add_scalars(main_tag='mutual_information/val',
                                    tag_scalar_dict={
                                        'I(Z;Y)': izy_bound.item(),
                                        'I(Z;X)': izx_bound.item()},
                                    global_step=self.global_iter)

        self.set_mode('train')

    