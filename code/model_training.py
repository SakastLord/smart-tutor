##########################################################################
# Training and Saving Models for Experiments
# Usually, use test_mcts.ipynb to get a first sense of training parameters
# then come here and use these functions to train and checkpoint models
##########################################################################

import numpy as np
import scipy as sp
import tensorflow as tf
import tflearn

import time
import copy
import pickle
import multiprocessing as mp
import six
import os
import random
import itertools

import constants
import data_generator as dg
import concept_dependency_graph as cdg
import student as st
import dynamics_model_class as dmc
import dataset_utils

from simple_mdp import SimpleMDP
from joblib import Parallel, delayed

# helper functions
from helpers import *
from simple_mdp import create_custom_dependency

# extract out the training states
class ExtractCallback(tflearn.callbacks.Callback):
    '''
    Used to get the training/validation losses after model.fit.
    '''
    def __init__(self):
        self.tstates = []
    def on_epoch_begin(self,ts):
        self.tstates.append([])
    def on_batch_end(self,ts,snapshot):
        self.tstates[-1].append(copy.copy(ts))

def _dkt_train_models_chunk(params, runstartix, chunk_num_runs):
    '''
    Loads data and trains a batch of models.
    A batch is a continguous sequence of runs
    '''
    
    #six.print_('startix {} nruns {}'.format(runstartix,chunk_num_runs))
    
    train_losses = [[] for _ in six.moves.range(chunk_num_runs)]
    val_losses = [[] for _ in six.moves.range(chunk_num_runs)]
    
    #load data
    data = dataset_utils.load_data(filename='{}{}'.format(dg.SYN_DATA_DIR, params.datafile))
    input_data_, output_mask_, target_data_ = dataset_utils.preprocess_data_for_rnn(data)
    
    for offset in six.moves.range(chunk_num_runs):
        r = runstartix + offset
        
        # new model instantiation
        dkt_model = dmc.DynamicsModel(model_id=params.model_id, timesteps=params.seqlen-1, dropout=params.dropout, output_dropout=params.output_dropout, load_checkpoint=False)
        
        epochs_trained = 0
        for ep in params.saved_epochs:
            print('=====================================')
            print('---------- Rep {:2d} Epoch {:2d} ----------'.format(r, ep))
            print('=====================================')
            
            # remember the epochs are given as zero-based
            epochs_to_train = ep+1 - epochs_trained
            assert epochs_to_train > 0
            
            # train
            ecall = ExtractCallback()
            
            for _ in six.moves.range(epochs_to_train):
                # add noise every epoch, so the noise is randomly different every epoch
                processed_input_data = input_data_ + (params.noise * np.random.randn(*input_data_.shape))
                train_data = (processed_input_data[:,:,:], output_mask_[:,:,:], target_data_[:,:,:])
                dkt_model.train(train_data, n_epoch=1, callbacks=ecall, shuffle=params.shuffle, load_checkpoint=False)
            
            # save the checkpoint
            checkpoint_name = params.checkpoint_pat.format(params.run_name, r, ep)
            checkpoint_path = '{}/{}'.format(params.dir_name,checkpoint_name)
            dkt_model.save(checkpoint_path)
            
            # update stats
            train_losses[offset].extend([np.mean([ts.global_loss for ts in batch]) for batch in ecall.tstates])
            val_losses[offset].extend([batch[-1].val_loss for batch in ecall.tstates])
            
            # update epochs_trained
            epochs_trained = ep+1
    return (train_losses, val_losses)

def dkt_train_models(params):
    '''
    Trains a bunch of random restarts of models, checkpointed at various epochs.
    '''
    
    # first try to create the checkpoint directory if it doesn't exist
    try:
        os.makedirs(params.dir_name)
    except:
        # do nothing if already exists
        pass
    
    train_losses = []
    val_losses = []
    
    n_jobs = min(5, params.num_runs) # seems like there are problems on windows with multiple threads
    # need to be a multiple of number of jobs so I don't have to deal with uneven leftovers
    assert(params.num_runs % n_jobs == 0)
    runs_per_job = int(params.num_runs / n_jobs)
    
    losses = list(Parallel(n_jobs=n_jobs)(delayed(_dkt_train_models_chunk)(params,startix,runs_per_job)
                                          for startix in six.moves.range(0,params.num_runs,runs_per_job)))
    
    for tloss, vloss in losses:
        train_losses.extend(tloss)
        val_losses.extend(vloss)
    #six.print_((train_losses,val_losses))
    
    # save stats
    stats_path = '{}/{}'.format(params.dir_name,params.stat_name)
    np.savez(stats_path,tloss=train_losses, vloss=val_losses,eps=params.saved_epochs)


################################ memoize the model predictions #####################################

def dkt_memoize_single_recurse(n_concepts, dkt, horizon, step, history_ix, mem_arrays):
    '''
    Recursively populate mem_arrays with the predictions of the dkt.
    :param dkt: the RnnStudentSim at the current history state
    :param horizon: the horizon of planning
    :param step: the next time step about to be memoized
    :param history_ix: the index of the current history
    :param mem_arrays: a list of memoization arrays per history length, with nothing at index 0
    '''
    # debugging print
    if False:
        six.print_('Current history: {}'.format(dkt.sequence))
        six.print_('Prediction: {}'.format(dkt.sample_observations()))
        six.print_('Memoized Prediction: {}'.format(mem_arrays[step-1][history_ix]))
    
    if step > horizon:
        # we've finished
        return
    
    # go over all possible next steps in the history and populate recursively
    for next_action in six.moves.range(n_concepts):
        for next_ob in (0,1):
            next_branch = action_ob_encode(n_concepts, next_action, next_ob)
            next_history_ix = history_ix_append(n_concepts, history_ix, next_branch)
            # advance the DKT
            next_dkt = dkt.copy()
            next_dkt.advance_simulator(st.make_student_action(n_concepts,next_action),next_ob)
            # add new entry to the mem arrays
            mem_arrays[step][next_history_ix,:] = next_dkt.sample_observations()
            # recurse
            dkt_memoize_single_recurse(n_concepts, next_dkt, horizon, step+1, next_history_ix, mem_arrays)

def dkt_memoize_single(n_concepts, model_id, checkpoint, horizon, outfile):
    '''
    Memoize a single given model up to and including the given horizon.
    :param checkpoint: a checkpoint file with the model
    :param horizon: the horizon of planning
    :param outfile: str name of the output file for mem arrays
    '''
    # load up the model
    dmodel = dmc.DynamicsModel(model_id, timesteps=horizon, load_checkpoint=False)
    dmodel.load(checkpoint)
    # wrap
    dkt = dmc.RnnStudentSim(dmodel)
    
    # compute the number of branches i.e |num actions|*2
    index_base = n_concepts * 2
    
    # initialize all to zero
    mem_arrays = [None] * (horizon+1)
    for i in six.moves.range(horizon+1):
        mem_arrays[i] = np.zeros((num_histories(index_base,i),n_concepts))
    
    # start populating the mem arrays recursive
    dkt_memoize_single_recurse(n_concepts, dkt, horizon, 1, 0, mem_arrays)
    
    # finished so write it
    np.savez(outfile, mem_arrays=mem_arrays)

def dkt_memoize_chunk(params, runstartix, chunk_num_runs):
    for offset in six.moves.range(chunk_num_runs):
        r = runstartix + offset
        
        for ep in params.saved_epochs:
            print('=====================================')
            print('---------- Rep {:2d} Epoch {:2d} ----------'.format(r, ep))
            print('=====================================')
            
            # compute checkpoint name
            checkpoint_name = params.checkpoint_pat.format(params.run_name, r, ep)
            checkpoint_path = '{}/{}'.format(params.dir_name,checkpoint_name)
            
            # compute outfile name
            mem_name = params.mem_pat.format(params.run_name, r, ep)
            mem_path = '{}/{}'.format(params.dir_name,mem_name)
            
            # memoize
            dkt_memoize_single(params.n_concepts, params.model_id, checkpoint_path, params.mem_horizon, mem_path)
            
            six.print_('Finished.')

def dkt_memoize_models(params):
    '''
    Takes the trained models, and memoizes all of their possible outputs for
    all possible histories up to length horizon, and dumps those arrays to a file.
    Each length of history is in its own array.
    Histories are indexed by treating them as numbers.
    History is [(action,ob),(action,ob),...] and (action,ob) is converted to a number
    and then the history is just treated as a number with a different base of |num action|*2
    '''
    n_jobs = min(5, params.num_runs) # seems like there are problems on windows with multiple threads
    # need to be a multiple of number of jobs so I don't have to deal with uneven leftovers
    assert(params.num_runs % n_jobs == 0)
    runs_per_job = int(params.num_runs / n_jobs)
    
    ignore = list(
        Parallel(n_jobs=n_jobs)(delayed(dkt_memoize_chunk)(params,startix,runs_per_job) 
                                for startix in six.moves.range(0,params.num_runs,runs_per_job)))

############################################################################
# multistep errors

def dkt_multistep_single(n_concepts, n_trajectories, model_id, checkpoints, horizon, use_mem):
    '''
    Compute the multistep error per step for either both random and expert policies.
    '''
    if not use_mem:
        model_list = []
        for chkpt in checkpoints:
            model = dmc.DynamicsModel(model_id, timesteps=horizon, load_checkpoint=False)
            model.load(chkpt)
            model_list.append(model)
        dkt = dmc.RnnStudentSimEnsemble(model_list)
    else:
        mem_array_list = []
        for chkpt in checkpoints:
            mem_arrays = np.load(chkpt)['mem_arrays']
            mem_array_list.append(mem_arrays)
        dkt = dmc.RnnStudentSimMemEnsemble(n_concepts, mem_array_list)
    
    concept_tree = cdg.ConceptDependencyGraph()
    concept_tree.init_default_tree(n_concepts)
    test_student = st.Student2(n_concepts, True)
    
    # for both policies
    policies = ['random', 'expert']
    
    # for horizons: 0,1,2,3,...,horizon
    errors = np.zeros((2,horizon+1))
    
    for pol in six.moves.range(2):
        for i in six.moves.range(n_trajectories):
            curr_dkt = dkt.copy()
            # sample a real trajectory
            traj = dg.generate_student_sample(concept_tree,seqlen=horizon+1,student=test_student,policy=policies[pol])
            for t in six.moves.range(horizon+1):
                curr_action = st.make_student_action_vec(traj[t][0])
                curr_ob = traj[t][1]
                curr_probs = sanitize_probs(n_concepts, curr_dkt.sample_observations())
                
                # advance dkt with sampled observation
                sampled_ob = 1 if np.random.random() < curr_probs[curr_action.concept] else 0
                curr_dkt.advance_simulator(curr_action, sampled_ob)
                
                # accumulate the error for this step
                errors[pol,t] += np.square(curr_ob - curr_probs[curr_action.concept])
    
    if False:
        six.print_('errors {}'.format(errors / n_trajectories))
    
    return errors / n_trajectories
                
        
def dkt_multistep_single_wrapper(params, n_trajectories, horizon, use_mem, checkpoints, runs, ep):
    print('=====================================')
    print('Started runs {} Epoch {:2d} ----------'.format(runs, ep))
    print('=====================================')
    outdata = dkt_multistep_single(params.n_concepts, n_trajectories, params.model_id, checkpoints, horizon, use_mem)
    print('Finished runs {} Epoch {:2d} ----------'.format(runs, ep))
    return outdata

def dkt_multistep(params, n_trajectories, horizon, use_mem):
    '''
    Computes the multistep error for every model up to the horizon.
    '''
    n_jobs = 8
    
    # get the checkpoints and model indices
    worker_inputs = []
    # outdata[modelix][ep][policy,horizon] = error
    outdata = [[] for _ in six.moves.range(params.num_runs)]
    for r in six.moves.range(params.num_runs):
        for ep in params.saved_epochs:
            if not use_mem:
                # compute checkpoint name
                checkpoint_name = params.checkpoint_pat.format(params.run_name, r, ep)
                checkpoint_path = '{}/{}'.format(params.dir_name,checkpoint_name)
            else:
                # compute outfile name
                mem_name = params.mem_pat.format(params.run_name, r, ep)
                checkpoint_path = '{}/{}'.format(params.dir_name,mem_name)
            
            worker_inputs.append(([r],ep,[checkpoint_path]))
    
    flat_output = list(
        Parallel(n_jobs=n_jobs)(delayed(dkt_multistep_single_wrapper)(params, n_trajectories, horizon, use_mem, checkpoints, runs, ep)
                                for runs,ep,checkpoints in worker_inputs))
    
    ix = 0
    for r in six.moves.range(params.num_runs):
        for ep in params.saved_epochs:
            outdata[r].append(flat_output[ix])
            ix+=1
    
    statfile = 'multistep-{}-horizon{}-n{}.pickle'.format(params.run_name, horizon,n_trajectories)
    statpath = '{}/{}'.format(params.dir_name,statfile)
    with open(statpath, 'wb') as f:
        pickle.dump(outdata,f)
    
def dkt_multistep_ensemble(trainparams,testparams):
    '''
    TODO
    Given a set of runs, test ensemble models with forward search
    '''
    
    fsdata = []
    
    for en in six.moves.range(testparams.ensemble_split):
        # compute how many runs to use
        curr_num_runs = int((en+1) * trainparams.num_runs / testparams.ensemble_split)
        fsdata.append([])
        for ep in trainparams.saved_epochs:
            print('=================================================')
            print('---------- Split {:1d}/{:1d} Runs {} Epoch {:2d} ----------'.format(en+1,testparams.ensemble_split, curr_num_runs, ep))
            print('=================================================')
            
            # create the checkpoints of all models
            curr_checkpoints = []
            for r in six.moves.range(curr_num_runs):
                if not testparams.use_mem:
                    checkpoint_name = trainparams.checkpoint_pat.format(trainparams.run_name, r, ep)
                    checkpoint_path = '{}/{}'.format(trainparams.dir_name,checkpoint_name)
                else:
                    mem_name = trainparams.mem_pat.format(trainparams.run_name, r, ep)
                    checkpoint_path = '{}/{}'.format(trainparams.dir_name,mem_name)
                curr_checkpoints.append(checkpoint_path)
            
            # do the forward search
            returned_data = dkt_forwardsearch_single(
                trainparams.n_concepts, trainparams.model_id, curr_checkpoints, testparams.horizon, testparams.use_mem)
            
            # update stats
            fsdata[-1].append(returned_data)
            
    
    # save stats
    stat_name = 'fsearchensemble-{}-horizon{}.pickle'.format(trainparams.run_name, testparams.horizon)
    stats_path = '{}/{}'.format(trainparams.dir_name,stat_name)
    with open(stats_path, 'wb') as f:
        pickle.dump(fsdata,f)

############################################################################
# Parameters for training models and saving them
############################################################################
class TrainParams(object):
    '''
    Parameters for training models. These are the ones corresponding to student2.
    '''
    def __init__(self, rname, nruns, model_id, seqlen, saved_epochs, dropout=1.0,noise=0.0,output_dropout=1.0):
        self.model_id = model_id
        self.n_concepts = 4
        self.transition_after = True
        self.dropout = dropout
        self.output_dropout = output_dropout
        self.shuffle = True
        # variance of gaussian noise added to the input
        self.noise = noise
        self.seqlen = seqlen
        self.datafile = 'test2a-w{}-n100000-l{}-random.pickle'.format(self.n_concepts, self.seqlen)
        # which epochs (zero-based) to save, the last saved epoch is the total epoch
        self.saved_epochs = saved_epochs
        # name of these runs, which should be unique to one call to train models (unless you want to overwrite)
        self.run_name = rname
        # how many runs
        self.num_runs = nruns
        
        # memoization horizon
        self.mem_horizon = 6

        # these names are derived from above and should not be touched generally
        noise_str = '-noise{:.2f}'.format(self.noise) if self.noise > 0.0 else ''
        
        output_dropout_str = '-outputdropout{:.2f}'.format(self.output_dropout) if self.output_dropout < 0.99 else ''
        
        # folder to put the checkpoints into
        self.dir_name = 'experiments/{}{}-dropout{}{}-shuffle{}-data-{}'.format(
            self.model_id,noise_str,int(self.dropout*10),output_dropout_str,int(self.shuffle),self.datafile)
        
        # pattern for the checkpoints
        self.checkpoint_pat = 'checkpoint-{}{}-epoch{}'
        
        # training stat file
        self.stat_name = 'stats-{}'.format(self.run_name)
        
        # memoized file pattern
        self.mem_pat = 'mem-{}{}-epoch{}.npz'
