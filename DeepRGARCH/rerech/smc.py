import numpy as np
import scipy as sp 
from rerech import resampling as rs

#####################################
# Particles
class ThetaParticles(object):

    def __init__(self, shared=None, **fields):
        self.shared = {} if shared is None else shared
        self.__dict__.update(fields)

    @property
    def N(self):
        return len(next(iter(self.dict_fields.values())))

    @property
    def dict_fields(self):
        return {k: v for k, v in self.__dict__.items() if k != 'shared'}

    def __getitem__(self, key):
        fields = {k: v[key] for k, v in self.dict_fields.items()}
        if isinstance(key, int):
            return fields
        else:
            return self.__class__(shared=self.shared.copy(), **fields)

    def __setitem__(self, key, value):
        for k, v in self.dict_fields.item():
            v[key] = getattr(value, k)

    def copy(self):
        """Returns a copy of the object."""
        fields = {k: v.copy() for k, v in self.dict_fields.items()}
        return self.__class__(shared=self.shared.copy(), **fields)

    @classmethod
    def concatenate(cls, *xs):
        fields = {k: gen_concatenate(*[getattr(x, k) for x in xs])
                  for k in xs[0].dict_fields.keys()}
        return cls(shared=xs[0].shared.copy(), **fields)

    def copyto(self, src, where=None):

        for k, v in self.dict_fields.items():
            if isinstance(v, np.ndarray):
                # takes care of arrays with ndims > 1
                wh = np.expand_dims(where, tuple(range(1, v.ndim)))
                np.copyto(v, getattr(src, k), where=wh)
            else:
                v.copyto(getattr(src, k), where=where)

    def copyto_at(self, n, src, m):

        for k, v in self.dict_fields.items():
            v[n] = getattr(src, k)[m]

#####################################
# MCMC
def view_2d_array(theta):
    """Returns a view to record array theta which behaves
    like a (N,d) float array.
    """
    v = theta.view(float)
    N = theta.shape[0]
    v.shape = (N, - 1)
    # raise an error if v cannot be reshaped without creating a copy
    return v

class AdaptiveMCMC(object):
    """MCMC sequence for a standard SMC sampler (keep only final states).
    """

    def __init__(self, len_chain=10, adaptive=False, delta_dist=0.1, cstr_fn=None, lstm=False):
        self.nsteps = len_chain - 1
        self.adaptive = adaptive
        self.delta_dist = delta_dist
        self.cstr_fn = cstr_fn
        self.lstm = lstm

    def __call__(self, x, target):
        xout = x.copy()
        ars = [] # accept rate list
        accept0 = np.zeros(x.N, dtype=bool)
        dist = 0.
        for _ in range(self.nsteps):  # if adaptive, nsteps is max nb of steps
            ar, accept = self.step(xout, target)
            ars.append(ar)
            accept0 = accept0|accept
        prev_ars2 = x.shared.get('acc_rates2', [])
        xout.shared['acc_rates2'] = prev_ars2 + [np.mean(accept0)] 
        prev_ars = x.shared.get('acc_rates', [])
        xout.shared['acc_rates'] = prev_ars + [ars]  # a list of lists
        return xout

    def calibrate(self, W, x):
        arr = view_2d_array(x.theta)
        N, d = arr.shape
        m, cov = rs.wmean_and_cov(W, arr)
        scale = 2.38 / np.sqrt(d)
        if self.lstm:
            cov = 0.0001*np.identity(d)+cov
        x.shared['chol_cov'] = scale * sp.linalg.cholesky(cov, lower=True)


    def step(self, x, target=None):
        xprop = x.__class__(theta=np.empty_like(x.theta)) 
        self.proposal(x, xprop)
        target(xprop)
        lp_acc = xprop.lpost - x.lpost 
        pb_acc = np.exp(np.clip(lp_acc, None, 0.))
        pb_acc[np.isnan(pb_acc)] = 0
        mean_acc = np.mean(pb_acc)
        if self.cstr_fn is None:
            accept = (np.random.rand(x.N) < pb_acc) 
        else:
            accept = (np.random.rand(x.N) < pb_acc) & self.cstr_fn(xprop.theta)
        x.copyto(xprop, where=accept)
        return mean_acc, accept

    def proposal(self, x, xprop):
        L = x.shared['chol_cov']
        arr = view_2d_array(x.theta)
        arr_prop = view_2d_array(xprop.theta)
        arr_prop[:, :] = (arr + sp.stats.norm.rvs(size=arr.shape) @ L.T)
        return 0.

#####################################
# SMC
class SMC(object):


    def __init__(self,
                 prior=None,
                 data=None,
                 cstr_fn=None,
                 verbose=False,
                 N=1000,                 
                 len_chain=30,
                 ESSrmin_=0.9, # for adaptvie termpering
                 mcmc=None,
                 resampling="systematic",
                 tdist=False,
                 lstm=True):

        self.N = N
        self.resampling = resampling
        self.ESSrmin_ = ESSrmin_ # for adaptive tempering
        self.verbose = verbose
        self.len_chain = len_chain
        self.cstr_fn = cstr_fn
        self.prior = prior

        # initialisation
        self.rs_flag = False  # no resampling at time 0, by construction
        self.mcmc = AdaptiveMCMC(len_chain=len_chain, cstr_fn=cstr_fn,lstm=lstm) if mcmc == None else mcmc(len_chain=len_chain)
        self.wgts = rs.Weights() # no weight, which means equal weight
        self.X, self.Xp, self.A = None, None, None # X is the particles
        self.t = 0
        self.tdist = tdist
        self.X_ls, self.wgts_ls, self.var_ls, self.w_ls = [],[],[],[]

    def __next__(self):
        if self.done():
            self.loglik(self.X.theta, get_v=True)
            raise StopIteration
        if self.t == 0:
            self.generate_particles()
        else:
            self.resample_move()
        self.reweight_particles()
        self.wgts_ls.append(self.wgts)
        if self.verbose:
            print("t={}, accept_rate={:.2f}, accept_rate2={:.2f}, epn={:.3f}".format(self.t, np.average(self.X.shared['acc_rates'][-1]),self.X.shared['acc_rates2'][-1],self.X.shared['exponents'][-1]))
        self.t += 1

    def done(self):
        if self.X is None:
            return False  # We have not started yet
        else:
            return self.X.shared['exponents'][-1] >= 1.

    def generate_particles(self):
        N0 = self.N
        if self.cstr_fn == None:
            x0 = ThetaParticles(theta=self.prior.rvs(size=N0))
        else:
            theta = self.prior.rvs(size=N0)
            theta = theta[self.cstr_fn(theta)]
            n = theta.shape[0]
            while n < N0:     
                theta_p = self.prior.rvs(size=N0)
                theta_p = theta_p[self.cstr_fn(theta_p)] 
                theta = np.concatenate((theta, theta_p))
                n+=theta_p.shape[0]
            theta = theta[:N0]
            x0 = ThetaParticles(theta=theta)
        x0.shared['acc_rates2'] = [0.]
        x0.shared['acc_rates'] = [0.]
        x0.shared['exponents'] = [0.]
        self.current_target(0.)(x0)
        self.X = x0

    def current_target(self, epn): # # update lpost or return fn for calculate lpost
        def func(x):
            x.lprior = self.prior.logpdf(x.theta)
            x.llik = self.loglik(x.theta)
            if epn > 0.:
                x.lpost = x.lprior + epn * x.llik
            else:  # avoid having 0 x Nan
                x.lpost = x.lprior.copy()
        return func

    def resample_move(self):
        self.rs_flag = True # We *always* resample in tempering
        if self.rs_flag:  # if resampling
            # calculate cov matrix
            self.mcmc.calibrate(self.wgts.W, self.X)  
            # resample
            self.A = rs.resampling(self.resampling, self.wgts.W, M=self.N) 
            self.Xp = self.X[self.A]
            self.wgts = rs.Weights() # reset to equal weight
            # move
            epn = self.Xp.shared['exponents'][-1]  # Xp is resampled X now, wait for MCMC move
            target = self.current_target(epn)
            self.X = self.mcmc(self.Xp, target)
        else:
            self.A = np.arange(self.N)
            self.Xp = self.X

    def reweight_particles(self):
        # calculate new epn
        ESSmin = self.ESSrmin_ * self.X.N
        f = lambda e: rs.essl(e * self.X.llik) - ESSmin
        epn = self.X.shared['exponents'][-1]
        if f(1. - epn) > 0:  # we're done (last iteration)
            delta = 1. - epn
            new_epn = 1. # set 1. manually so that we can safely test == 1.
        else:
            delta = sp.optimize.brentq(f, 1.e-12, 1. - epn)  # secant search
            # left endpoint is >0, since f(0.) = nan if any likelihood = -inf
            new_epn = epn + delta
        self.X.shared['exponents'].append(new_epn)
        # calculate delta llik
        dllik = delta * self.X.llik
        self.X.lpost += dllik
        #update weights
        self.wgts = self.wgts.add(dllik)

    def __iter__(self):
        return self

    def run(self):
        for _ in self:
            pass

#####################################
# SMC
class SMCD(object):


    def __init__(self,
                 verbose=False,            
                 len_chain=30,
                 ESSrmin=0.9, # for resampling
                 mcmc=None,
                 resampling="systematic",
                 pre=None):

        self.resampling = resampling
        self.verbose = verbose
        self.ESSrmin = ESSrmin

        self.pre = pre
        self.cstr_fn = pre.cstr_fn
        self.N = pre.N
        self.prior = pre.prior
        self.tdist = pre.tdist
        self.mcmc = pre.mcmc

        # initialisation
        self.rs_flag = True  # no resampling at time 0
        self.X, self.Xp, self.A = None, None, None # X is the particles
        self.t = 0
        self.wgts_ls = [pre.wgts]
        self.X_ls = [pre.X]
        self.var_ls,self.w_ls = [],[]

        # debug
        self.ESS = 0

    def __next__(self):
        if self.done():
            self.var_ls = np.array(self.var_ls)
            raise StopIteration
        if self.t == 0:
            self.generate_particles()
        else:
            self.resample_move()
        self.reweight_particles()
        self.wgts_ls.append(self.wgts)
        self.X_ls.append(self.X)
        if self.verbose&self.rs_flag&self.t>0:
            print("t={}, ESS={:.3f},accept_rate={:.2f},accept_rate2={:.2f}".format(self.t,self.ESS,np.average(self.X.shared['acc_rates'][-1]),self.X.shared['acc_rates2'][-1]))
        self.t+=1

    def done(self):
        return self.t >= self.T

    def generate_particles(self):
        N0 = self.N
        x0 = ThetaParticles(theta=self.pre.X.theta)
        self.wgts = rs.Weights(lw=self.pre.wgts.lw)
        x0.shared['acc_rates'] = [0.]
        x0.shared['exponents'] = [0.]
        self.current_target(-1)(x0)
        self.X = x0       

    def current_target(self, t):
        def func(x):
            x.lpost = self.prior.logpdf(x.theta) + self.loglik(x.theta, t)
        return func

    def reweight_particles(self):
        lpyt = self.loglik(self.X.theta, t=self.t, lpyt=True)
        self.wgts = self.wgts.add(lpyt)
        self.X.lpost += lpyt

    def resample_move(self):
        self.ESS = self.wgts.ESS
        self.rs_flag = (self.wgts.ESS < self.X.N * self.ESSrmin)
        if self.rs_flag:
            # calibrate cov matrix
            self.mcmc.calibrate(self.wgts.W, self.X) 
            # resample
            self.A = rs.resampling(self.resampling, self.wgts.W, M=self.N)
            self.Xp = self.X[self.A]
            self.wgts = rs.Weights()
            # move
            target = self.current_target(self.t-1) # move with t-1
            self.X = self.mcmc(self.Xp, target)
        else:
            self.A = np.arange(self.N)
            self.Xp = self.X

    def __iter__(self):
        return self

    def run(self):
        for _ in self:
            pass

