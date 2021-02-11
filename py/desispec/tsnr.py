import os
import numpy as np
import astropy.io.fits as fits
import glob
import numpy as np

from desispec.io.spectra import Spectra
from astropy.convolution import convolve, Box1DKernel
from scipy.interpolate import RectBivariateSpline
from specter.psf.gausshermite  import  GaussHermitePSF
from desispec.calibfinder import findcalibfile
from desiutil.log import get_logger
from scipy.optimize import minimize


def get_ensemble(dirpath, bands, smooth=125):
    '''
    Function that takes a frame object and a bitmask and
    returns ivar (and optionally mask) array(s) that have fibers with
    offending bits in fibermap['FIBERSTATUS'] set to
    0 in ivar and optionally flips a bit in mask.

    Args:
        dirpath: path to the dir. with ensemble dflux files. 
        bands:  bands to expect, typically [BRZ] - case ignored.

    Options:
        smooth:  Further convolve the residual ensemble flux. 

    Returns:
        Dictionary with keys labelling each tracer (bgs, lrg, etc.) for which each value
        is a Spectra class instance with wave, flux for BRZ arms.  Note flux is the high 
        frequency residual for the ensemble.  See doc. 4723.
    '''
    paths = glob.glob(dirpath + '/tsnr-ensemble-*.fits')

    wave = {}
    flux = {}
    ivar = {}
    mask = {}
    res  = {}

    ensembles  = {}

    for path in paths:
        tracer = path.split('/')[-1].split('-')[2].replace('.fits','')
        dat    = fits.open(path)

        for band in bands:
            wave[band] = dat['WAVE_{}'.format(band.upper())].data
            flux[band] = dat['DFLUX_{}'.format(band.upper())].data
            ivar[band] = 1.e99 * np.ones_like(flux[band])

            # 125: 100. A in 0.8 pixel.
            if smooth > 0:
                flux[band] = convolve(flux[band][0,:], Box1DKernel(smooth), boundary='extend')
                flux[band] = flux[band].reshape(1, len(flux[band]))
                
        ensembles[tracer] = Spectra(bands, wave, flux, ivar)
        
    return  ensembles

def read_nea(path):
    '''
    Read a master noise equivalent area [sq. pixel] file.  

    input:
        path: path to a master nea file for a given camera, e.g. b0.
 
    returns:
        nea: 2D split object to be evaluated at (fiber, wavelength)
        angperpix:  2D split object to be evaluated at (fiber, wavelength),
                    yielding angstrom per pixel. 
    '''
    
    with fits.open(path, memmap=False) as fx:
        wave=fx['WAVELENGTH'].data
        angperpix=fx['ANGPERPIX'].data
        nea=fx['NEA'].data

    fiber = np.arange(len(nea))

    nea = RectBivariateSpline(fiber, wave, nea)
    angperpix = RectBivariateSpline(fiber, wave, angperpix)

    return  nea, angperpix

def fb_rdnoise(fibers, frame, psf):
    '''
    Approximate the readnoise for a given fiber (on a given camera) for the 
    wavelengths present in frame. wave.

    input:
        fibers: e.g. np.arange(500) to index fiber. 
        frame:  frame instance for the given camera.
        psf:  corresponding psf instance. 

    returns:
        rdnoise: (nfiber x nwave) array with the estimated readnosie.  Same 
                 units as OBSRDNA, e.g. ang per pix.
    '''
    
    ccdsizes = np.array(frame.meta['CCDSIZE'].split(',')).astype(np.float)

    xtrans = ccdsizes[0] / 2.
    ytrans = ccdsizes[1] / 2.

    rdnoise = np.zeros_like(frame.flux)

    for ifiber in fibers:
        wave_lim = psf.wavelength(ispec=ifiber, y=ytrans)
        x = psf.x(ifiber, wave_lim)

        # A | C.
        if x < xtrans:
            rdnoise[ifiber, frame.wave <  wave_lim] = frame.meta['OBSRDNA']
            rdnoise[ifiber, frame.wave >= wave_lim] = frame.meta['OBSRDNC']

        # B | D
        else:
            rdnoise[ifiber, frame.wave <  wave_lim] = frame.meta['OBSRDNB']
            rdnoise[ifiber, frame.wave >= wave_lim] = frame.meta['OBSRDND']

    return rdnoise

def var_model(rdnoise_sigma, npix_1d, angperpix, angperspecbin, fiberflat, skymodel, alpha=1.0, components=False):
    '''
    Evaluate a model for the 1D spectral flux variance, e.g. quadrature sum of readnoise and sky components.  

    input:
        rdnoise_sigma:
        npix_1d:  equivalent to (1D) nea. 
        angperpix:  Angstroms per pixel.
        angperspecbin: Angstroms per bin.       
        fiberflat: fiberflat instance
        skymodel: Sky instance.
        alpha: empirical weighting of the rdnoise term to e.g. better fit sky fibers per exp. cam.    
        components:  if True, return tuple of individual contributions to the variance.  Else return variance. 

    returns: 
        nfiber x nwave array of the expected variance. 
    '''
    
    # the extraction is performed with a wavelength bin of width = angperspecbin
    # so the effective number of CCD pixels corresponding to a spectral bin width is
    npix_2d = npix_1d * (angperspecbin / angperpix)

    # then, the extracted flux per specbin is converted to an extracted flux per A, so
    # the variance has to be divided by the square of the conversion factor = angperspecbin**2

    rdnoise_variance = rdnoise_sigma**2 * npix_2d / angperspecbin**2

    # It was verified that this variance has to be increased by about 10% to match the
    # inverse variance reported in the frame files of a zero exposure (exptime=0).
    # However the correction factor (alpha) can be larger when fitted on sky fibers
    # because the precomputed effective noise equivalent number of pixels (npix_1d)
    # is valid only when the Poisson noise is negligible. It increases with the spectral flux.

    if components:
        return (alpha * rdnoise_variance, fiberflat.fiberflat * np.abs(skymodel.flux))

    else:
        return alpha * rdnoise_variance + fiberflat.fiberflat * np.abs(skymodel.flux)

def calc_alpha(frame, fibermap, rdnoise_sigma, npix_1d, angperpix, angperspecbin, fiberflat, skymodel):
    '''
    Model Var = alpha * rdnoise component + sky.

    Calculate the best-fit alpha using the sky fibers
    available to the frame.

    input:
        frame: desispec frame instance (should be uncalibrated, i.e. e/A).
        fibermap: desispec fibermap instance.
        rdnoise_sigma:  e.g. RDNOISE value per Quadrant (float). 
        npix_1d:  equivalent to 1D nea [pixels], calculated using read_nea().
        angperpix:  angstroms per pixel (float),
        fiberflat: desispec fiberflat instance.
        skymodel: desispec Sky instance.
        alpha:  nuisanve parameter to reweight rdnoise vs sky contribution to variance (float).
        components:  if True, return individual contributions to variance, else return total variance. 

    returns:
       alpha:  nuisance parameter to reweight rdnoise vs sky contribution to variance (float), obtained 
               as the best fit to the uncalibrated sky fibers VAR.
    '''

    sky_indx = np.where(fibermap['OBJTYPE'] == 'SKY')[0]
    rd_var, sky_var = var_model(rdnoise_sigma, npix_1d, angperpix, angperspecbin, fiberflat, skymodel, alpha=1.0, components=True)

    maskfactor = np.ones_like(frame.mask[sky_indx,:], dtype=np.float)
    maskfactor[frame.mask[sky_indx,:] > 0] = 0.0
    
    def calc_alphavar(alpha):
        return alpha * rd_var[sky_indx,:] + sky_var[sky_indx,:]

    def alpha_X2(alpha):
        _var = calc_alphavar(alpha)
        _ivar =  1. / _var
        X2 = (frame.ivar[sky_indx,:] - _ivar)**2.
        return np.sum(X2 * maskfactor)

    res = minimize(alpha_X2, x0=[1.])
    alpha = res.x[0]

    try:
        assert  alpha >= 0.0

    except AssertionError:
        log.critical('Best-fit alpha for tsnr calc. is negative.')
        
    return alpha

#- Cache files from desimodel to avoid reading them N>>1 times
_camera_nea_angperpix = None
_band_ensemble = None

def calc_tsnr2(frame, fiberflat, skymodel, fluxcalib) :
    '''
    Compute template SNR^2 values for a given frame

    Args:
        frame : uncalibrated Frame object for one camera
        fiberflat : FiberFlat object
        sky : SkyModel object
        fluxcalib : FluxCalib object

    returns (tsnr2, alpha):
        `tsnr2` dictionary, with keys labeling tracer (bgs,elg,etc.), of values
        holding nfiber length array of the tsnr^2 values for this camera, and
        `alpha`, the relative weighting btwn rdnoise & sky terms to model var.

    Note:  Assumes DESIMODEL is set and up to date. 
    '''
    global _camera_nea_angperpix
    global _band_ensemble
    
    log=get_logger()

    if not (frame.meta["BUNIT"]=="count/Angstrom" or frame.meta["BUNIT"]=="electron/Angstrom" ) :
        log.error("requires an uncalibrated frame")
        raise RuntimeError("requires an uncalibrated frame")

    camera=frame.meta["CAMERA"].strip().lower()
    band=camera[0]

    psfpath=findcalibfile([frame.meta],"PSF")
    psf=GaussHermitePSF(psfpath)

    # Returns bivariate spline to be evaluated at (fiber, wave).
    if not "DESIMODEL" in os.environ :
        log.error("requires the environment variable DESIMODEL to get the NEA and the SNR templates")
        raise RuntimeError("requires the environment variable DESIMODEL to get the NEA and the SNR templates")

    if _camera_nea_angperpix is None:
        _camera_nea_angperpix = dict()

    if camera in _camera_nea_angperpix:
        nea, angperpix = _camera_nea_angperpix[camera]
    else:
        neafilename=os.path.join(os.environ["DESIMODEL"],
                                 f"data/specpsf/nea/masternea_{camera}.fits")
        log.info("read NEA file {}".format(neafilename))
        nea, angperpix = read_nea(neafilename)
        _camera_nea_angperpix[camera] = nea, angperpix

    if _band_ensemble is None:
        _band_ensemble = dict()

    if band in _band_ensemble:
        ensemble = _band_ensemble[band]
    else:
        ensembledir=os.path.join(os.environ["DESIMODEL"],"data/tsnr")
        log.info("read TSNR ensemble files in {}".format(ensembledir))
        ensemble = get_ensemble(ensembledir, bands=[band,])
        _band_ensemble[band] = ensemble

    nspec, nwave = fluxcalib.calib.shape

    fibers = np.arange(nspec)
    rdnoise = fb_rdnoise(fibers, frame, psf)

    # Evaluate.
    npix = nea(fibers, frame.wave)
    angperpix = angperpix(fibers, frame.wave)
    angperspecbin = np.mean(np.gradient(frame.wave))

    for label, x in zip(['RDNOISE', 'NEA', 'ANGPERPIX', 'ANGPERSPECBIN'], [rdnoise, npix, angperpix, angperspecbin]):
        log.info('{} \t {:.3f} +- {:.3f}'.format(label.ljust(10), np.median(x), np.std(x)))

    # Relative weighting between rdnoise & sky terms to model var.
    alpha = calc_alpha(frame, fibermap=frame.fibermap, rdnoise_sigma=rdnoise, npix_1d=npix, angperpix=angperpix, angperspecbin=angperspecbin, fiberflat=fiberflat, skymodel=skymodel)
    log.info("TSNR ALPHA = {:4.3f}".format(alpha))

    maskfactor = np.ones_like(frame.mask, dtype=np.float)
    maskfactor[frame.mask > 0] = 0.0
    
    tsnrs = {}

    denom = var_model(rdnoise, npix, angperpix, angperspecbin, fiberflat, skymodel, alpha=alpha)
    
    for tracer in ensemble.keys():
        wave = ensemble[tracer].wave[band]
        dflux = ensemble[tracer].flux[band]

        if len(frame.wave) != len(wave) or not np.allclose(frame.wave, wave):
            log.warning(f'Resampling {tracer} ensemble wavelength to match input {camera} frame')
            dflux = np.interp(frame.wave, wave, dflux,
                              left=dflux[0], right=dflux[-1])
            wave = frame.wave

        # Work in uncalibrated flux units (electrons per angstrom); flux_calib includes exptime. tau.
        # Broadcast.
        dflux = dflux * fluxcalib.calib # [e/A]

        # Wavelength dependent fiber flat;  Multiply or divide - check with Julien.
        result = dflux * fiberflat.fiberflat
        result = result**2.

        result /= denom
        
        # Eqn. (1) of https://desi.lbl.gov/DocDB/cgi-bin/private/RetrieveFile?docid=4723;filename=sky-monitor-mc-study-v1.pdf;version=2
        tsnrs[tracer] = np.sum(result * maskfactor, axis=1)

    results=dict()
    for tracer in tsnrs.keys():
        key = 'TSNR2_{}_{}'.format(tracer.upper(), band.upper())
        results[key]=tsnrs[tracer]
        log.info('{} = {:.6f}'.format(key, np.median(tsnrs[tracer])))

    return results, alpha
