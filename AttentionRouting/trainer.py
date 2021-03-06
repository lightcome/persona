
import os
import shutil
import random
import math
import time
import argparse
import yaml
from collections import OrderedDict

import sys
# for import parent utils
sys.path.append('../')
import utils
import modules
import datasets
import models

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader, ConcatDataset


class Trainer:
    def __init__(self):
        args = self.parse_args()
        self.args = args
        self.best_valid_loss = float('inf')
        self.device = utils.get_device(args.device)
        utils.set_random_seed(self.args.seed, self.device)

        self.ensure_deps()

        self.grad_util = utils.Grads()

        self.logger = utils.create_logger(self.args.log_path, 'trainer')

        print('Build vocab and embeddings...')
        self.build_vocab_and_embeddings()
        print('Build dataloaders...')
        self.build_dataloaders()
        print('Build model...')
        self.build_model()
        print('Build loss fns...')
        self.build_loss_fns()
        
    def parse_args(self):
        parser = argparse.ArgumentParser()

        parser.add_argument('--config_file', default='configs/default.yaml', type=str, required=False, 
            help='Provide config in config_file or as other commandline args')
        parser.add_argument('--experiment_name', default='', type=str, required=False, help='')
        parser.add_argument('--device', default='cuda', type=str, required=False, help='use cpu for easy debug')
        parser.add_argument('--seed', default=42, type=int, required=False, help='')
        parser.add_argument('--n_epochs', default=10, type=int, required=False, help='')
        parser.add_argument('--n_epochs_early_stage', default=0, type=int, required=False, help='')
        parser.add_argument('--batch_size', default=128, type=int, required=False, help='')
        parser.add_argument('--limit_example_length', default=256, type=int, required=False, help='')
        parser.add_argument('--max_seq_length', default=300, type=int, required=False, help='')
        parser.add_argument('--max_context_size', default=10, type=int, required=False, help='')
        parser.add_argument('--shuffle_data', action='store_true', required=False, help='')
        parser.add_argument('--max_vocab_size', default=40000, type=int, required=False, help='')
        parser.add_argument('--pretrain_emb', action='store_true', required=False, help='')

        parser.add_argument('--emb_freeze', action='store_true', required=False, help='')
        parser.add_argument('--enc_dropout', default=0.1, type=float, required=False, help='')
        parser.add_argument('--dec_dropout', default=0.1, type=float, required=False, help='')
        parser.add_argument('--num_layers', default=6, type=int, required=False, help='')
        parser.add_argument('--n_head', default=8, type=int, required=False, help='')
        parser.add_argument('--d_model', default=512, type=int, required=False, help='')
        parser.add_argument('--d_ff', default=2048, type=int, required=False, help='')
        parser.add_argument('--attn_alpha', default=1, type=int, required=False, help='')
        parser.add_argument('--alpha', default=0.5, type=float, required=False, help='LM loss weight')

        parser.add_argument('--lr', default=0.5, type=float, required=False, help='')
        parser.add_argument('--weight_decay', default=0.99, type=float, required=False, help='')
        parser.add_argument('--clip_grad', default=1, type=int, required=False, help='')

        parser.add_argument('--model_path', default='models/', type=str, required=False, help='')
        parser.add_argument('--pretrained_path', type=str, required=False, help='')
        parser.add_argument('--data_path', default='datas/', type=str, required=False, help='')
        parser.add_argument('--cache_path', default='caches/', type=str, required=False, help='')
        parser.add_argument('--log_path', default='logs/', type=str, required=False, help='')
        parser.add_argument('--corpus_fname', default='datas/corpus.txt', type=str, required=False, help='')
        parser.add_argument('--vec_fname', default='models/vec.txt', type=str, required=False, help='')
        parser.add_argument('--vocab_fname', default='models/vocab.txt', type=str, required=False, help='')

        # TODO: let commandline temp args override args in config_file
        args = parser.parse_args()
        if args.config_file != '':
            parser.set_defaults(**yaml.load(open(args.config_file)))
            args = parser.parse_args()

        return args

    def ensure_deps(self):
        if self.args.pretrain_emb:
            try:
                v = '3.8.3'
                import gensim
                assert gensim.__version__ >= v
            except:
                raise Exception('If pretrain_emb enabled, please install gensim>=%s' % v)

    def build_vocab_and_embeddings(self):
        args = self.args

        if args.pretrain_emb and (
                not os.path.exists(args.vec_fname) 
                or not os.path.exists(args.vocab_fname)
        ):
            print('Pretraining word2vec...')
            utils.build_word2vec(args.corpus_fname, args.vec_fname, args.vocab_fname, 
                    max_vocab_size=args.max_vocab_sizem, emb_dim=args.d_model)

        embeddings, gensim_vocab = None, None
        if args.pretrain_emb:
            print('Loading word2vec...')
            embeddings, gensim_vocab = utils.load_embeddings_and_vocab(args.vec_fname, args.vocab_fname)
            embeddings = embeddings.to(self.device)
        self.vocab = utils.Vocab(gensim_vocab, args.data_path)
        self.input_dim = len(self.vocab)
        # if special_tokens did not include in pretrain_emb, append zeros, disable emb_freeze
        if args.pretrain_emb:
            elen = embeddings.shape[0]
            if self.input_dim > elen:
                args.emb_freeze = False
                append = torch.zeros(self.input_dim - elen, embeddings.shape[1]).to(self.device)
                embeddings = torch.cat([embeddings, append], dim=0)

        self.pad_idx = self.vocab.stoi(utils.PAD)
        self.embeddings = embeddings
                                                                                         
    def build_dataloaders(self):
        args = self.args
        gb = lambda batch: datasets.generate_batch(batch, self.pad_idx)
        gb_lm = lambda batch: datasets.generate_lm_batch(batch, self.pad_idx)

        if args.n_epochs_early_stage > 0:
            dp = datasets.LMDataProcesser(limit_length=args.limit_example_length, 
                    max_seq_length=args.max_seq_length)
            ds = utils.PersonaDataset(
                    self.vocab, args.max_seq_length, args.limit_example_length, 
                    data_path=args.data_path, cache_path=args.cache_path, 
                    data_processer=dp, mode='train_lm')
            self.train_iter = DataLoader(ds, batch_size=args.batch_size, 
                    collate_fn=gb_lm, shuffle=True) 
        else:
            dp = datasets.ChatDataProcesser(limit_length=args.limit_example_length, 
                    max_seq_length=args.max_seq_length, max_context_size=args.max_context_size)
            ds = utils.PersonaDataset(
                    self.vocab, args.max_seq_length, args.limit_example_length, 
                    data_path=args.data_path, cache_path=args.cache_path, 
                    data_processer=dp, mode='train_char')
            self.train_iter = DataLoader(ds, batch_size=args.batch_size, 
                    collate_fn=gb, shuffle=args.shuffle_data) 

        self.valid_iter = None
        self.test_iter = None
        ds = utils.PersonaDataset(
                self.vocab, args.max_seq_length, args.limit_example_length, 
                data_path=args.data_path, cache_path=args.cache_path, 
                data_processer=dp, mode='valid_char')
        self.valid_iter = DataLoader(ds, batch_size=args.batch_size,
                collate_fn=gb, shuffle=args.shuffle_data) 

        ds = utils.PersonaDataset(
                self.vocab, args.max_seq_length, args.limit_example_length, 
                data_path=args.data_path, cache_path=args.cache_path, 
                data_processer=dp, mode='test_char')
        self.test_iter = DataLoader(ds, batch_size=args.batch_size,
                collate_fn=gb, shuffle=args.shuffle_data)

    def build_model(self):
        args = self.args
        output_dim = self.input_dim
        input_dim = self.input_dim

        self.best_model = None
        self.model = models.AR.build(args, input_dim, 
                output_dim, self.vocab, self.embeddings).to(self.device)
 
        self.optimizer = optim.AdamW(self.model.parameters(), lr=args.lr,
                weight_decay=args.weight_decay)
        print(self.model)
        print(f'The model has {utils.count_parameters(self.model):,} trainable parameters') 

        self.scheduler = optim.lr_scheduler.StepLR(self.optimizer, 1.0, gamma=0.95)

        if args.pretrained_path is None:
            pass
            # pytorch module will auto init_weights with uniform
            # self.model.apply(models.init_weights)
        else:
            print()
            print(f'Load pretrained model {args.pretrained_path}...')
            self.load_model()

    def build_loss_fns(self):
        self.out_loss_fn = nn.CrossEntropyLoss(ignore_index=self.pad_idx)

    def run_early_stage(self):
        for epoch in range(self.args.n_epochs_early_stage):
            start_time = time.time()

            train_loss = self.train_lm(epoch)
            self.save_model(epoch, 'lm')

            end_time = time.time()
            epoch_mins, epoch_secs = epoch_time(start_time, end_time)
         
            self.logger.info('-' * 89)
            self.logger.info('Experiment %s: ' % self.args.experiment_name)
            self.logger.info(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
            self.logger.info(f'\tTrain Loss: {train_loss:.3f} | Train PPL: {math.exp(train_loss):7.3f}')

    def run(self):
        if self.args.n_epochs_early_stage > 0:
            print('Run early stage...')
            trainer.run_early_stage()
            # after fin, rerun with pretrained model 
            return

        print('Run main stage...')

        best_val_loss = float("inf")

        for epoch in range(self.args.n_epochs):
            start_time = time.time()

            train_loss = self.train(epoch)
            valid_loss = self.eval(self.valid_iter)
 
            if valid_loss < best_val_loss:
                best_val_loss = valid_loss
                self.best_model = self.model
                self.save_model(epoch)

            # scheduler.step()

            end_time = time.time()
            epoch_mins, epoch_secs = epoch_time(start_time, end_time)

            self.logger.info('-' * 89)
            self.logger.info('Experiment %s: ' % self.args.experiment_name)
            self.logger.info(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
            self.logger.info(f'\tTrain Loss: {train_loss:.3f} | Train PPL: {math.exp(train_loss):7.3f}')
            self.logger.info(f'\t Val. Loss: {valid_loss:.3f} |  Val. PPL: {math.exp(valid_loss):7.3f}')

        test_loss = self.eval(self.test_iter)
        self.logger.info(f'| Test Loss: {test_loss:.3f} | Test PPL: {math.exp(test_loss):7.3f} |')

        self.grad_util.plot()

    def train_lm(self, epoch):
        self.model.train()

        epoch_loss = 0
        for batch_idx, feature in enumerate(self.train_iter):
            self.optimizer.zero_grad()

            utils.feature_to_device(feature, self.device)

            out = self.model(feature)
            loss = self.out_loss_fn(out.view(-1, out.shape[-1]), 
                    feature.y.view(-1))
            # utils.print_backward_graph(loss)
            loss.backward()

            nn.utils.clip_grad_norm_(self.model.parameters(), self.args.clip_grad)
            self.optimizer.step()

            iloss = loss.item()
            epoch_loss += iloss
            self.logger.info(f'Step {batch_idx+1}/{epoch+1:02} | Train Loss: {iloss:.3f} | Train PPL: {math.exp(iloss):7.3f} | Time: {secs:.3f}s\n')

        return epoch_loss / len(self.train_iter)
 

    def train(self, epoch, data_iter=None):
        self.model.train()

        if data_iter is None:
            data_iter = self.train_iter

        epoch_loss = 0
        for batch_idx, feature in enumerate(data_iter):
            start_time = time.time()

            self.optimizer.zero_grad()

            utils.feature_to_device(feature, self.device)

            out, out_lm = self.model(feature)
            loss, loss_lm = models.AR.loss(self.out_loss_fn, 
                    out, out_lm, feature.resp, feature.lm.y)
            loss = loss + self.args.alpha * loss_lm

            # utils.print_backward_graph(loss)
            loss.backward()

            nn.utils.clip_grad_norm_(self.model.parameters(), self.args.clip_grad)
            self.grad_util.collect(self.model)

            self.optimizer.step()

            iloss = loss.item()
            epoch_loss += iloss

            end_time = time.time()
            secs = end_time - start_time
            self.logger.info(f'Step {batch_idx+1}/{epoch+1:02} | Train Loss: {iloss:.3f} | Train PPL: {math.exp(iloss):7.3f} | Time: {secs:.3f}s\n')

        return epoch_loss / len(data_iter)

    def eval(self, data_iter=None):
        self.model.eval()

        if data_iter is None:
            data_iter = self.test_iter

        epoch_loss = 0
        with torch.no_grad():
            for _, feature in enumerate(data_iter):

                utils.feature_to_device(feature, self.device)

                out, out_lm = self.model(feature)
                loss, loss_lm = models.AR.loss(self.out_loss_fn, 
                        out, out_lm, feature.resp, feature.lm.y)
                loss = loss + self.args.alpha * loss_lm

                epoch_loss += loss.item()

        return epoch_loss / len(data_iter)

    # resuming vs Warmstarting(transfer learning)?
    # it just have difference of optimizer state_dict
    # https://pytorch.org/tutorials/beginner/saving_loading_models.html#saving-loading-a-general-checkpoint-for-inference-and-or-resuming-training
    def save_model(self, epoch, stage=''):
        model_path = os.path.join(self.args.model_path, 
                'model_{}_epoch{}'.format(stage, epoch + 1))
        if not os.path.exists(model_path):
            os.mkdir(model_path)
        torch.save(self.model.state_dict(), model_path + '/model.pt')
        shutil.copyfile(self.args.config_file, model_path + '/config.yml')
        shutil.copyfile(self.args.vocab_fname, model_path + '/vocab')

    def load_model(self):
        self.model.load_state_dict(torch.load(self.args.pretrained_path))


def epoch_time(start_time: int, end_time: int):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs


if __name__ == '__main__':
    trainer = Trainer()
    trainer.run()

