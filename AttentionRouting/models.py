
import math
import random

import torch
import torch.nn as nn

import sys
# for import parent utils
sys.path.append('../')
import utils

import modules



class AR(nn.Module):
    def __init__(
        self,
        context_emb,
        persona_emb,
        output_emb,
        post_encoder,
        resp_decoder,
        generater,
    ):
        super().__init__()

        self.context_emb = context_emb
        self.persona_emb = persona_emb
        self.output_emb = output_emb
        self.post_encoder = post_encoder
        self.resp_decoder = resp_decoder
        self.generater = generater

        self._share_emb()
        self._share_encoder_decoder()
        _init_weights(self.parameters())

    def forward(self, feature):
        context_enc, persona_enc = self.encode(feature)
        out = self.decode(feature, context_enc, persona_enc)

        return out

    def encode(self, feature):
        context_emb = self.context_emb(feature)
        persona_emb = self.persona_emb(feature.persona)

        context_enc = self.post_encoder(context_emb, feature.context_pad_mask)
        persona_enc = self.post_encoder(persona_emb, feature.persona_pad_mask)
 
        return context_enc, persona_enc

    def decode(self, feature, context_enc, persona_enc):
        resp_enc = self.output_emb(feature.resp)
        out = self.resp_decoder(resp_enc, context_enc, persona_enc, 
                memory_key_padding_mask=feature.context_pad_mask, 
                tgt_mask=feature.resp_mask, 
                tgt_key_padding_mask=feature.resp_pad_mask, 
                persona_pad_mask=feature.persona_pad_mask) 

        enc_lm = self.output_emb(feature.lm.x)
        out_lm = self.resp_decoder(enc_lm, tgt_mask=feature.lm.x_mask,
                tgt_key_padding_mask=feature.lm.x_pad_mask) 

        return self.generate(out), self.generate(out_lm)

    def generate(self, enc):
        return self.generater(enc)

    def predict(self, X):
        max_len = y.shape[0]

        post_outs, post_hid = self.encoder(X)
        trait_enc = self.trait_encoder(profiles)

        hid = post_hid
        out = y[start]
        rnn_outs = None
        outs = torch.zeros(*y.shape[:2], 
                self.resp_decoder.output_dim).to(X.device)
        for t in range(start, end):
            trait_fus = self.trait_fusion(hid, trait_enc)
            out, hid, _ = self.resp_decoder(out, hid, post_outs, trait_fus)
            outs[t] = out
            teacher_force = random.random() < teacher_forcing_ratio
            top1 = out.max(1)[1]
            out = y[t] if teacher_force else top1

        return outs

    def _share_emb(self):
        self.context_emb.emb = self.output_emb.emb
        self.persona_emb.emb = self.output_emb.emb
        self.generater.out.weight = self.output_emb.emb.weight

    def _share_encoder_decoder(self):
        for i, layer in enumerate(self.post_encoder.transformer_encoder.layers):
            d_layer = self.resp_decoder.layers[i]
            layer.self_attn = d_layer.multihead_attn
            layer.linear1 = d_layer.linear1
            layer.linear2 = d_layer.linear2
            layer.norm1 = d_layer.norm1
            layer.norm2 = d_layer.norm2


class LM(AR):
    def forward(self, feature):
        enc = self.output_emb(feature.x)
        out = self.resp_decoder(enc, tgt_mask=feature.x_mask) 
        return self.generater(out)
 

class _LM(nn.Module):
    def __init__(
        self,
        output_emb,
        decoder,
        generater,
    ):
        super().__init__()

        self.output_emb = output_emb
        self.decoder = decoder
        self.generater = generater

        self._share_emb()
        _init_weights(self.parameters())

    def forward(self, feature):
        enc = self.output_emb(feature.x)
        out = self.decoder(enc, tgt_mask=feature.x_mask) 
        return self.generater(out)

    def _share_emb(self):
        self.generater.out.weight = self.output_emb.emb.weight
       
def _init_weights(m):
    for p in m:
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)


def init_weights(m):
    for name, param in m.named_parameters():
        if 'weight' in name:
            n = param.data.shape[-1]
            nn.init.uniform_(param.data, -math.sqrt(3/n), math.sqrt(3/n))
            # nn.init.normal_(param.data, mean=0, std=0.01)
        else:
            nn.init.constant_(param.data, 0)


def count_parameters(m):
    return sum(p.numel() for p in m.parameters() if p.requires_grad)

 

def build_word2vec(corpus_fname, vec_fname, vocab_fname, 
        max_vocab_size, trim_rule=utils.vocab_zh_trim_rule, emb_dim=100):
    """
    no need utils.vocab_zh_trim_rule for char embedding
    """
    import gensim
    lss = gensim.models.word2vec.LineSentence(corpus_fname) 
    # skip-gram is more accuracy for most words, but CBOW is better for name similarity
    model = gensim.models.Word2Vec(lss, 
            max_final_vocab=max_vocab_size, size=emb_dim,
            trim_rule=trim_rule)
    model.wv.save_word2vec_format(vec_fname, vocab_fname)
    return model


def load_embeddings_and_vocab(vec_fname, vocab_fname):
    import gensim
    model = gensim.models.KeyedVectors.load_word2vec_format(vec_fname, vocab_fname)
    return torch.tensor(model.vectors), model.vocab
