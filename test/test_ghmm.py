from __future__ import print_function
import numpy as np
from mixtape.ghmm import GaussianFusionHMM

PLOT = True

def test_viterbi():
    data = [np.random.randn(1000, 3) + np.tile(np.sin(np.arange(1000)/100.0), (3,1)).T]

    model1 = GaussianFusionHMM(n_states=2, n_features=3, platform='sklearn').fit(data)
    model2 = GaussianFusionHMM(n_states=2, n_features=3, platform='cpu').fit(data)

    model2.means_ = model1.means_
    model2.vars_ = model1.vars_
    model2.transmat_ = model1.transmat_
    model2.populations_ = model1.populations_

    logprob1, seq1 = model1.predict(data)
    logprob2, seq2 = model2.predict(data)

    np.testing.assert_almost_equal(logprob1, logprob2, decimal=3)
    np.testing.assert_array_equal(seq1[0], seq2[0])


    if PLOT:
        import matplotlib.pyplot as pp
        pp.plot(data[0][:, 0], label='data')
        pp.plot(seq1[0], lw='5', label='viterbi')
        pp.legend()
        pp.show()
