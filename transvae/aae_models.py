import os
import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import math, copy, time
from torch.autograd import Variable

from transvae.tvae_util import *
from transvae import loss
from transvae.opt import NoamOpt, AdamOpt, AAEOpt
from transvae.trans_models import VAEShell, Generator, ConvBottleneck, DeconvBottleneck, PropertyPredictor, Embeddings, LayerNorm

import torch.distributed as dist
import torch.utils.data.distributed
from transvae.DDP import *

class AAE(VAEShell):
    """
    AAE architecture
    Bypass_bottleneck is set to True and thus the VAE variational or reparaemterization will be avoided
    """
    def __init__(self, params={}, name=None, N=3, d_model=128,
                 d_latent=128, dropout=0.1, tf=True,
                 bypass_bottleneck=True, property_predictor=False,
                 d_pp=256, depth_pp=2, type_pp='deep_net', load_fn=None, discriminator_layers=[640, 256]):
        super().__init__(params, name)


        ### Set learning rate for Adam optimizer
        if 'ADAM_LR' not in self.params.keys():
            self.params['ADAM_LR'] = 3e-4

        ### Store architecture params
        self.model_type = 'aae'
        self.params['model_type'] = self.model_type
        self.params['N'] = N
        self.params['d_model'] = d_model
        self.params['d_latent'] = d_latent
        self.params['dropout'] = dropout
        self.params['teacher_force'] = tf
        self.params['bypass_bottleneck'] = bypass_bottleneck
        self.params['property_predictor'] = property_predictor
        self.params['type_pp'] = type_pp
        self.params['d_pp'] = d_pp
        self.params['depth_pp'] = depth_pp
        self.params['discriminator_layers'] = discriminator_layers
        self.arch_params = ['N', 'd_model', 'd_latent', 'dropout', 'teacher_force', 'bypass_bottleneck',
                            'property_predictor', 'd_pp', 'depth_pp']

        ### Build model architecture
        if load_fn is None:
            if self.params['DDP']:
                DDP_init(self)
            else:
                self.build_model()
        else:
            self.load(load_fn)

    def build_model(self):
        """
        Build model architecture. This function is called during initialization as well as when
        loading a saved model checkpoint
        """
        self.device = torch.device("cuda" if 'gpu' in self.params['HARDWARE'] else "cpu")
        encoder = RNNEncoder(self.params['d_model'], self.params['d_latent'], self.params['N'],
                             self.params['dropout'], self.params['bypass_bottleneck'], self.device)
        decoder = RNNDecoder(self.params['d_model'], self.params['d_latent'], self.params['N'],
                             self.params['dropout'], 125, self.params['teacher_force'], self.params['bypass_bottleneck'],
                             self.device)
        """ADDING DISCRIMINATOR with proper latent size and number of discriminator layers"""
        discriminator = Discriminator(self.params['d_latent'], self.params['discriminator_layers'])
        
        generator = Generator(self.params['d_model'], self.vocab_size)
        src_embed = Embeddings(self.params['d_model'], self.vocab_size)
        tgt_embed = Embeddings(self.params['d_model'], self.vocab_size)
        if self.params['property_predictor']:
            property_predictor = PropertyPredictor(self.params['d_pp'], self.params['depth_pp'], self.params['d_latent'],
                                                  self.params['type_pp'])
        else:
            property_predictor = None
        self.model = RNNEncoderDecoder(encoder, decoder, discriminator, src_embed, tgt_embed, generator,
                                       property_predictor, self.params)
        for p in self.model.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        self.use_gpu = torch.cuda.is_available()
        if 'gpu' in self.params['HARDWARE']:
            self.model.cuda()
            self.params['CHAR_WEIGHTS'] = self.params['CHAR_WEIGHTS'].cuda()

        ### Initiate optimizers
        #named_parameters returns tuple: (str, params) , store all except discriminator params in 1st opt, store discriminator in 2nd opt
        self.optimizer = AAEOpt(params=[p[1] for p in self.model.named_parameters() 
                                         if (p[1].requires_grad and not "discriminator" in p[0])],
                                disc_params=[p[1] for p in self.model.named_parameters() 
                                         if (p[1].requires_grad and "discriminator" in p[0])],
                                lr=self.params['ADAM_LR'], 
                                generator_optimizer=optim.Adam,
                                discriminator_optimizer=optim.Adam)

        

########## Recurrent Sub-blocks ############

class RNNEncoderDecoder(nn.Module):
    """
    Recurrent Encoder-Decoder Architecture
    """
    def __init__(self, encoder, decoder, discriminator, src_embed, tgt_embed, generator,
                 property_predictor, params):
        super().__init__()
        self.params = params
        self.encoder = encoder
        self.decoder = decoder
        self.discriminator = discriminator
        self.src_embed = src_embed
        self.tgt_embed = tgt_embed
        self.generator = generator
        self.property_predictor = property_predictor

    def forward(self, src, tgt, true_prop, weights, beta, optimizer, train_test, src_mask=None, tgt_mask=None):
        mem, mu, logvar = self.encode(src) # the mem is the latent space from the encoder
        x, h = self.decode(tgt, mem)
        x = self.generator(x)
        if self.property_predictor is not None:
            prop = self.predict_property(mem, true_prop) # the vae bottleneck is bypassed so the "mem" is storing the latent memory
        else:
            prop = None
        tot_loss, bce, kld, prop_bce, disc_loss = loss.aae_loss(src, x, mu, logvar,
                                                                  true_prop, prop,
                                                                  weights,
                                                                  self, mem, optimizer, train_test, beta)
        
        
        return tot_loss, bce, kld, prop_bce, disc_loss #since the loss is already computed the outputs are the loss outputs

    def encode(self, src):
        return self.encoder(self.src_embed(src))

    def decode(self, tgt, mem):
        return self.decoder(self.tgt_embed(tgt), mem)

    def predict_property(self, mem, true_prop):
        return self.property_predictor(mem, true_prop)


class RNNEncoder(nn.Module):
    """
    Simple recurrent encoder architecture
    """
    def __init__(self, size, d_latent, N, dropout, bypass_bottleneck, device):
        super().__init__()
        self.size = size
        self.n_layers = N
        self.bypass_bottleneck = bypass_bottleneck
        self.device = device

        self.gru = nn.GRU(self.size, self.size, num_layers=N, dropout=dropout)
        self.norm = LayerNorm(size)
        """AAE does not use the std and logvar but will pass through a linear layer that will match the Moses AAE encoder output"""
        self.linear_bypass = nn.Linear(size, d_latent)

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return mu + eps*std

    def forward(self, x):
        h = self.initH(x.shape[0])
        x = x.permute(1, 0, 2)
        x, h = self.gru(x, h)
        mem = self.norm(h[-1,:,:])
        if self.bypass_bottleneck:
            mu, logvar = Variable(torch.tensor([0.0])), Variable(torch.tensor([0.0]))
            mem = self.linear_bypass(mem) #added linear_bypass 
        else:
            mu, logvar = self.z_means(mem), self.z_var(mem)
            mem = self.reparameterize(mu, logvar)
        return mem, mu, logvar

    def initH(self, batch_size):
        return torch.zeros(self.n_layers, batch_size, self.size, device=self.device)

class RNNDecoder(nn.Module):
    """
    Simple recurrent decoder architecture
    """
    def __init__(self, size, d_latent, N, dropout, tgt_length, tf, bypass_bottleneck, device):
        super().__init__()
        self.size = size
        self.n_layers = N
        self.max_length = tgt_length+1
        self.teacher_force = tf
        if self.teacher_force:
            self.gru_size = self.size * 2
        else:
            self.gru_size = self.size
        self.bypass_bottleneck = bypass_bottleneck
        self.device = device

        self.gru = nn.GRU(self.gru_size, self.size, num_layers=N, dropout=dropout)
        self.dropout = nn.Dropout(dropout)
        self.norm = LayerNorm(size)
        """AAE does not use the std and logvar but will pass through a linear layer that will match the Moses AAE encoder output"""
        self.linear_bypass = nn.Linear(d_latent, size)

    def forward(self, tgt, mem):
        h = self.initH(mem.shape[0])
        embedded = self.dropout(tgt)
        if not self.bypass_bottleneck:
            mem = F.relu(self.unbottleneck(mem))
            mem = mem.unsqueeze(1).repeat(1, self.max_length, 1)
            mem = self.norm(mem)
        else:
            mem = F.relu(self.linear_bypass(mem)) #added linear_bypass
            mem = mem.unsqueeze(1).repeat(1, self.max_length, 1)
            mem = self.norm(mem)
        if self.teacher_force:
            mem = torch.cat((embedded, mem), dim=2)
        mem = mem.permute(1, 0, 2)
        mem = mem.contiguous()
        x, h = self.gru(mem, h)
        x = x.permute(1, 0, 2)
        x = self.norm(x)
        return x, h

    def initH(self, batch_size):
        return torch.zeros(self.n_layers, batch_size, self.size, device=self.device)
    

class Discriminator(nn.Module):
    def __init__(self, input_size, layers):
        super().__init__()

        in_features = [input_size] + layers
        out_features = layers + [1]

        self.layers_seq = nn.Sequential()
        for k, (i, o) in enumerate(zip(in_features, out_features)):
            self.layers_seq.add_module('linear_{}'.format(k), nn.Linear(i, o))
            if k != len(layers):
                self.layers_seq.add_module('activation_{}'.format(k),
                                           nn.ELU(inplace=True))

    def forward(self, x):
        return self.layers_seq(x)
