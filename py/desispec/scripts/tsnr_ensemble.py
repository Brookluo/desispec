import sys
import copy
import pickle
import desisim
import argparse
import os.path                       as     path
import numpy                         as     np
import astropy.io.fits               as     fits
import desisim.templates

from   astropy.convolution           import convolve, Box1DKernel
from   pathlib                       import Path
from   desiutil.dust                 import mwdust_transmission
from   desiutil.log                  import get_logger


np.random.seed(seed=314)

# AR/DK DESI spectra wavelengths                                                                                                                                                                                                              
# TODO:  where are brz extraction wavelengths defined?  https://github.com/desihub/desispec/issues/1006.                                                                                                                                      
wmin, wmax, wdelta = 3600, 9824, 0.8
wave               = np.round(np.arange(wmin, wmax + wdelta, wdelta), 1)
cslice             = {"b": slice(0, 2751), "r": slice(2700, 5026), "z": slice(4900, 7781)}

def parse(options=None):
    parser = argparse.ArgumentParser(description="Generate a sim. template ensemble of given type.")
    parser.add_argument('--nmodel', type = int, default = 2000, required=True,
                        help='Number of galaxies in the ensemble.')
    parser.add_argument('--tracer', type = str, default = 'bgs', required=True,
                        help='Tracer to generate.')
    parser.add_argument('--outdir', type = str, default = 'bgs', required=True,
			help='Directory to write to.')
    args = None

    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)

    return args
        
class template_ensemble(object):
    '''                                                                                                                                                                                                                                   
    Generate an ensemble of templates to sample tSNR for a range of points in                                                                                                                                                             
    (z, m, OII, etc.) space.                                                                                                                                                                                                                                                                                                                                                                                                                                                       
    If conditioned, uses deepfield redshifts and (currently r) magnitudes to condition simulated templates.                                                                                                                               
    '''
    def __init__(self, outdir, tracer='ELG', nmodel=5):        
        def tracer_maker(wave, tracer=tracer, nmodel=nmodel, redshifts=None, mags=None):
            # https://arxiv.org/pdf/1611.00036.pdf
            if tracer == 'elg':
                maker = desisim.templates.ELG(wave=wave)
                zrange = (0.6, 1.6)
                magrange = (22.4, 23.4)
                tfilter='decam2014-r'

                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, trans_filter=tfilter, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)
                
            elif tracer == 'qso':
                maker = desisim.templates.QSO(wave=wave)
                zrange = (0.5, 3.0)
                magrange = (21.5, 22.5)
                tfilter='decam2014-r'

                # Does not recognize trans filter. 
                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)
                
            elif tracer == 'lrg':
                maker = desisim.templates.LRG(wave=wave)
                zrange = (0.7, 0.9)
                magrange = (20.5, 21.3)
                tfilter='decam2014-z'

                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, trans_filter=tfilter, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)
                
            elif tracer == 'bgs':
                maker = desisim.templates.BGS(wave=wave)
                zrange = (0.01, 0.4)
                magrange = (19.8, 20.0)
                tfilter='decam2014-r'

                flux, wave, meta, objmeta = maker.make_templates(nmodel=nmodel, trans_filter=tfilter, redshift=redshifts, mag=mags, south=True, zrange=zrange, magrange=magrange)
                
            else:
                raise  ValueError('{} is not an available tracer.'.format(tracer))

            return  wave, flux, meta, objmeta
        
        _, flux, meta, objmeta         = tracer_maker(wave, tracer=tracer, nmodel=nmodel)
                
        self.ensemble_flux             = {}
        self.ensemble_dflux            = {}
        self.ensemble_meta             = meta
        self.ensemble_objmeta          = objmeta
        self.ensemble_dflux_stack      = {}
        
        # Generate template (d)fluxes for brz bands.                                                                                                                                                                                          
        for band in ['b', 'r', 'z']:
            band_wave                     = wave[cslice[band]]

            in_band                       = np.isin(wave, band_wave)

            self.ensemble_flux[band]      = flux[:, in_band]

            dflux                         = np.zeros_like(self.ensemble_flux[band])
        
            # Retain only spectral features < 100. Angstroms.                                                                                                                                                                                 
            # dlambda per pixel = 0.8; 100A / dlambda per pixel = 125.                                                                                                                                                                        
            for i, ff in enumerate(self.ensemble_flux[band]):
                sflux                     = convolve(ff, Box1DKernel(125), boundary='extend')
                dflux[i,:]                = ff - sflux

            self.ensemble_dflux[band]     = dflux

        # Stack ensemble.
        for band in ['b', 'r', 'z']:
            self.ensemble_dflux_stack[band] = np.sqrt(np.mean(self.ensemble_dflux[band]**2., axis=0).reshape(1, len(self.ensemble_dflux[band].T)))

        hdr = fits.Header()
        hdr['NMODEL'] = nmodel
        hdr['TRACER'] = tracer

        hdu_list = [fits.PrimaryHDU(header=hdr)]

        for band in ['b', 'r', 'z']:
            hdu_list.append(fits.ImageHDU(wave[cslice[band]], name='WAVE_{}'.format(band.upper())))
            hdu_list.append(fits.ImageHDU(self.ensemble_dflux_stack[band], name='DFLUX_{}'.format(band.upper())))

        hdu_list = fits.HDUList(hdu_list)
            
        hdu_list.writeto('{}/tsnr-ensemble-{}.fits'.format(outdir, tracer), overwrite=True)
                                    
def main():
    log = get_logger()

    args = parse()
    
    rads = template_ensemble(args.outdir, tracer=args.tracer, nmodel=args.nmodel)

if __name__ == '__main__':
    main()
