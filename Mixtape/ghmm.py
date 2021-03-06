"""
`ghmm` implements a gaussian hidden Markov model with an optional
 pairwise L1 fusion penality on the means of the output distributions.
"""
# Author: Robert McGibbon <rmcgibbo@gmail.com>
# Contributors:
# Copyright (c) 2013, Stanford University
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#   Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
#   Redistributions in binary form must reproduce the above copyright notice, this
#   list of conditions and the following disclaimer in the documentation and/or
#   other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------
from __future__ import print_function, division

import time
import warnings
import numpy as np
from sklearn import cluster
_AVAILABLE_PLATFORMS = ['cpu', 'sklearn']
from mixtape import _ghmm, _reversibility
try:
    from mixtape import _cuda_ghmm_single
    from mixtape import _cuda_ghmm_mixed
    _AVAILABLE_PLATFORMS.append('cuda')
except ImportError:
    pass

EPS = np.finfo(np.float32).eps

#-----------------------------------------------------------------------------
# Code
#-----------------------------------------------------------------------------

class GaussianFusionHMM(object):
    """
    Reversible Gaussian Hidden Markov Model L1-Fusion Regularization

    Parameters
    ----------
    n_states : int
        The number of components (states) in the model
    n_em_iter : int
        The number of iterations of expectation-maximization to run
    n_lqa_iter : int
        The number of iterations of the local quadratic approximation fixed
        point equations to solve when computing the new means with a nonzero
        L1 fusion penalty.
    thresh : float
        Convergence threshold for the log-likelihood during expectation
        maximization. When the increase in the log-likelihood is less
        than thresh between subsequent rounds of E-M, fitting will finish.
    fusion_prior : float
        The strength of the L1 fusion prior.
    reversible_type : str
        Method by which the reversibility of the transition matrix
        is enforced. 'mle' uses a maximum likelihood method that is
        solved by numerical optimization (BFGS), and 'transpose'
        uses a more restrictive (but less computationally complex)
        direct symmetrization of the expected number of counts.
    transmat_prior : float, optiibal
        A prior on the transition matrix entries. If supplied, a
        psuedocount of transmat_prior - 1 is added to each entry
        in the expected number of observed transitions from each state
        to each other state, so this is like a uniform dirichlet alpha
        in a sense.
    vars_prior : float, optional
        A prior used on the variance. This can be useful in the undersampled
        regime where states may be collapsing onto a single point, but
        is generally not needed.
    vars_weight : float, optional
        Weight of the vars prior
    random_states : int, optional
        Random state, used during sampling.
    params : str
        A string with the parameters to optimizing during the fitting.
        If 't' is in params, the transition matrix will be optimized. If
        'm' is in params, the statemeans will be optimized. If 'v' is in
        params, the state variances will be optimized.
    init_params : str
        A string with the parameters to initialize prior to fitting.
        If 't' is in params, the transition matrix will be set. If
        'm' is in params, the statemeans will be set. If 'v' is in
        params, the state variances will be set.
    n_hotstart_sequences : int
        Number of sequences to use when hotstarting the EM with kmeans.
        Default=50

    Notes
    -----
    """
    def __init__(self, n_states, n_features, n_em_iter=100, n_lqa_iter=10,
                 fusion_prior=1e-2, thresh=1e-2, reversible_type='mle',
                 transmat_prior=None, vars_prior=1e-3, vars_weight=1,
                 random_state=None, params='tmv', init_params='tmv',
                 platform='cpu', precision='mixed', timing=True,
                 n_hotstart_sequences=50):
        self.n_states = n_states
        self.n_features = n_features
        self.n_em_iter = n_em_iter
        self.n_lqa_iter = n_lqa_iter
        self.fusion_prior = fusion_prior
        self.thresh = thresh
        self.reversible_type = reversible_type
        self.transmat_prior = transmat_prior
        self.vars_prior = vars_prior
        self.vars_weight = vars_weight
        self.random_state = random_state
        self.params = params
        self.init_params = init_params
        self.platform = platform
        self.timing = timing
        self.n_hotstart_sequences = n_hotstart_sequences
        self._impl = None

        if not reversible_type in ['mle', 'transpose']:
            raise ValueError('Invalid value for reversible_type: %s '
                             'Must be either "mle" or "transpose"'
                             % reversible_type)

        if self.platform == 'cpu':
            self._impl = _ghmm.GaussianHMMCPUImpl(self.n_states, self.n_features, precision)
        elif self.platform == 'sklearn':
            self._impl = _SklearnGaussianHMMCPUImpl(self.n_states, self.n_features)
        elif self.platform == 'cuda':
            if precision == 'single':
                self._impl = _cuda_ghmm_single.GaussianHMMCUDAImpl(self.n_states, self.n_features)
            elif precision == 'mixed':
                self._impl = _cuda_ghmm_mixed.GaussianHMMCUDAImpl(self.n_states, self.n_features)
            else:
                raise ValueError('Only single and mixed precision are supported on CUDA')
        else:
            raise ValueError('Invalid platform "%s". Available platforms are '
                             '%s.' % (platform, ', '.join(_AVAILABLE_PLATFORMS)))

        if self.transmat_prior is None:
            self.transmat_prior = 1.0

    def fit(self, sequences):
        """Estimate model parameters.

        An initialization step is performed before entering the EM
        algorithm. If you want to avoid this step, pass proper
        ``init_params`` keyword argument to estimator's constructor.

        Parameters
        ----------
        sequences : list
            List of 2-dimensional array observation sequences, each of which
            has shape (n_samples_i, n_features), where n_samples_i
            is the length of the i_th observation.
        """
        self._init(sequences, self.init_params)
        self.fit_logprob_ = []
        iterations_timing = []
        n_obs = sum(len(s) for s in sequences)

        for i in range(self.n_em_iter):
            if self.timing:
                iterations_timing.append(time.time())

            # Expectation step
            curr_logprob, stats = self._impl.do_estep()
            if stats['trans'].sum() > 10*n_obs:
                print('Number of transition counts', stats['trans'].sum())
                print('Total sequence length', n_obs)
                print("Numerical overflow detected. Try splitting your trajectories")
                print("into shorter segments or running in double")
                break


            self.fit_logprob_.append(curr_logprob)

            # Check for convergence
            if i > 0 and abs(self.fit_logprob_[-1] - self.fit_logprob_[-2]) < self.thresh:
                break

            # Maximization step
            self._do_mstep(stats, self.params)

        if self.timing:
            samples_per_s = sum(len(s) for s in sequences) / np.diff(iterations_timing)
            #print('GaussianHMM EM Fitting')
            #print('----------------------')
            #print('Platform: %s' % self.platform)
            #print('EM Iters: %s' % i)
            #print('Speed:    %.3f +/- %.3f us/sample' % (np.mean(us_per_sample_per_iter ), np.std(us_per_sample_per_iter )))
            self.mean_fit_time_ = np.mean(samples_per_s)
            self.std_fit_time_ = np.std(samples_per_s)

        return self

    def _init(self, sequences, init_params):
        '''
	Find initial means(hot start)
	'''
	self._impl._sequences = sequences

        small_dataset = np.vstack(sequences[0:min(len(sequences), self.n_hotstart_sequences)])

        if 'm' in init_params:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.means_ = cluster.KMeans(n_clusters=self.n_states).fit(small_dataset).cluster_centers_
        if 'v' in init_params:
            self.vars_ = np.vstack([np.var(small_dataset, axis=0)] * self.n_states)
        if 't' in init_params:
            transmat_ = np.empty((self.n_states, self.n_states))
            transmat_.fill(1.0 / self.n_states)
            self.transmat_ = transmat_
            self.populations_ = np.ones(self.n_states) / self.n_states

    def _do_mstep(self, stats, params):
        if 't' in params:
            if self.reversible_type == 'mle':
                counts = np.maximum(stats['trans'] + self.transmat_prior - 1.0, 1e-20).astype(np.float64)
                self.transmat_, self.populations_ = _reversibility.reversible_transmat(counts)
            elif self.reversible_type == 'transpose':
                revcounts = np.maximum(self.transmat_prior - 1.0 + stats['trans'] + stats['trans'].T, 1e-20)
                populations = np.sum(revcounts, axis=0)
                self.populations_ = populations / np.sum(populations)
                self.transmat_ = revcounts / np.sum(revcounts, axis=1)[:, np.newaxis]
            else:
                raise ValueError('Invalid value for reversible_type: %s '
                                 'Must be either "mle" or "transpose"'
                                 % self.reversible_type)

        difference_cutoff = 1e-10
        # we don't want denom to be zero, because then the new value of the means
        # will be nan/inf. so padd it up by a very small constant. This particular
        # padding is following the sklearn mixture model m_step code from
        # https://github.com/scikit-learn/scikit-learn/blob/master/sklearn/mixture/gmm.py#L496
        denom = (stats['post'][:, np.newaxis] + 10*EPS)

        def getdiff(means):
            diff = np.zeros((self.n_features, self.n_states, self.n_states))
            for i in range(self.n_features):
                diff[i] = np.maximum(np.abs(np.subtract.outer(means[:, i], means[:, i])), difference_cutoff)
            return diff

        if 'm' in params:
            means = stats['obs'] / denom  # unregularized means

            if self.fusion_prior > 0 and self.n_lqa_iter > 0:
                strength = self.fusion_prior / getdiff(means)  # adaptive regularization strength
                rhs =  stats['obs'] / self.vars_
                for i in range(self.n_features):
                    np.fill_diagonal(strength[i], 0)

                break_lqa = False
                for s in range(self.n_lqa_iter):
                    diff = getdiff(means)
                    if np.all(diff <= difference_cutoff) or break_lqa:
                        break

                    offdiagonal = -strength / diff
                    diagonal_penalty = np.sum(strength/diff, axis=2)
                    for f in range(self.n_features):
                        if np.all(diff[f] <= difference_cutoff):
                            continue
                        ridge_approximation = np.diag(stats['post'] / self.vars_[:, f] + diagonal_penalty[f]) + offdiagonal[f]
                        try:
                            means[:, f] = np.linalg.solve(ridge_approximation, rhs[:, f])
                        except np.linalg.LinAlgError:
                            # I'm not really sure what exactly causes the ridge
                            # approximation to be non-solvable, but it probably
                            # means we're too close to the merging. Maybe 1e-10
                            # is cutting it too close. ANyways, just break now and
                            # use the last valid value of the means.
                            break_lqa = True

                for i in range(self.n_features):
                    for k, j in zip(*np.triu_indices(self.n_states)):
                        if diff[i, k, j] <= difference_cutoff:
                            means[k, i] = means[j, i]

            self.means_ = means

        if 'v' in params:
            vars_prior = self.vars_prior
            vars_weight = self.vars_weight
            if vars_prior is None:
                vars_weight = 0
                vars_prior = 0

            var_num = (stats['obs**2']
                       - 2 * self.means_ * stats['obs']
                       + self.means_ ** 2 * denom)
            var_denom = max(vars_weight - 1, 0) + denom
            self.vars_ = (vars_prior + var_num) / var_denom

    @property
    def means_(self):
        return self._means_
    @means_.setter
    def means_(self, value):
        value = np.asarray(value, order='c', dtype=np.float32)
        self._means_ = value
        self._impl.means_ = value

    @property
    def vars_(self):
        return self._vars_
    @vars_.setter
    def vars_(self, value):
        value = np.asarray(value, order='c', dtype=np.float32)
        self._vars_ = value
        self._impl.vars_ = value

    @property
    def transmat_(self):
        return self._transmat_
    @transmat_.setter
    def transmat_(self, value):
        value = np.asarray(value, order='c', dtype=np.float32)
        self._transmat_ = value
        self._impl.transmat_ = value

    @property
    def populations_(self):
        return self._populations_
    @populations_.setter
    def populations_(self, value):
        value = np.asarray(value, order='c', dtype=np.float32)
        self._populations_ = value
        self._impl.startprob_ = value

    def timescales_(self):
        """The implied relaxation timescales of the hidden Markov transition
        matrix

        By diagonalizing the transition matrix, its propagation of an arbitrary
        initial probability vector can be written as a sum of the eigenvectors
        of the transition weighted by per-eigenvector term that decays
        exponentially with time. Each of these eigenvectors describes a
        "dynamical mode" of the transition matrix and has a characteristic
        timescale, which gives the timescale on which that mode decays towards
        equilibrium. These timescales are given by :math:`-1/log(u_i)` where
        :math:`u_i` are the eigenvalues of the transition matrix. In an HMM
        with N components, the number of non-infinite timescales is N-1. (The
        -1 comes from the fact that the stationary distribution of the chain
        is associated with an eigenvalue of 1, and an infinite characteritic
        timescale).

        Returns
        -------
        timescales : array, shape=[n_states-1]
            The characteristic timescales of the transition matrix. If the model
            has not been fit or does not have a transition matrix, the return
            value will be None. The timescales are ordered from longest to
            shortest.
        """
        if self.transmat_ is None:
            return None
        try:
            eigvals = np.linalg.eigvals(self.transmat_)
            eigvals = sorted(eigvals)[::-1]
            return -1.0 / np.log(eigvals[1:])
        except np.linalg.LinAlgError:
            # this can happen if the transition matrix contains Nans
            # or Infs, and possibly for other reasons like convergence
            return np.nan * np.ones(self.n_states - 1)

    def score(self, sequences):
        """Log-likelihood of sequences under the model

        Parameters
        ----------
        sequences : list
            List of 2-dimensional array observation sequences, each of which
            has shape (n_samples_i, n_features), where n_samples_i
            is the length of the i_th observation.
        """
        self._impl._sequences = sequences
        logprob, _ = self._impl.do_estep()
        return logprob

    def predict(self, sequences):
        """Find most likely hidden-state sequence corresponding to
        each data timeseries.

        Uses the Viterbi algorithm.

        Parameters
        ----------
        sequences : list
            List of 2-dimensional array observation sequences, each of which
            has shape (n_samples_i, n_features), where n_samples_i
            is the length of the i_th observation.

        Returns
        -------
        viterbi_logprob : float
            Log probability of the maximum likelihood path through the HMM.

        hidden_sequences : list of np.ndarrays[dtype=int, shape=n_samples_i]
            Index of the most likely states for each observation.
        """
        if not hasattr(self._impl, 'do_viterbi'):
            raise NotImplementedError(
                'The %s platform does not support this algorithm (yet)' %
                self.platform)

        self._impl._sequences = sequences
        logprob, state_sequences = self._impl.do_viterbi()

        return logprob, state_sequences


class _SklearnGaussianHMMCPUImpl(object):
    def __init__(self, n_states, n_features):
        from sklearn.hmm import GaussianHMM
        self.impl = GaussianHMM(n_states, params='stmc')

        self._sequences = None
        self.means_ = None
        self.vars_ = None
        self.transmat_ = None
        self.startprob_ = None


    def do_estep(self):
        from sklearn.utils.extmath import logsumexp

        self.impl.means_ = self.means_.astype(np.double)
        self.impl.covars_ = self.vars_.astype(np.double)
        self.impl.transmat_ = self.transmat_.astype(np.double)
        self.impl.startprob_ = self.startprob_.astype(np.double)
        stats = self.impl._initialize_sufficient_statistics()
        curr_logprob = 0
        for seq in self._sequences:
            seq = seq.astype(np.double)
            framelogprob = self.impl._compute_log_likelihood(seq)
            lpr, fwdlattice = self.impl._do_forward_pass(framelogprob)
            bwdlattice = self.impl._do_backward_pass(framelogprob)
            gamma = fwdlattice + bwdlattice
            posteriors = np.exp(gamma.T - logsumexp(gamma, axis=1)).T
            curr_logprob += lpr
            self.impl._accumulate_sufficient_statistics(
                stats, seq, framelogprob, posteriors, fwdlattice,
                bwdlattice, self.impl.params)

        return curr_logprob, stats


    def do_viterbi(self):
        logprob = 0
        state_sequences = []
        for obs in self._sequences:
            lpr, ss = self.impl._decode_viterbi(obs)
            logprob += lpr
            state_sequences.append(ss)

        return logprob, state_sequences
