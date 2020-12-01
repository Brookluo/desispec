"""
Run PSF estimation.
"""

from __future__ import print_function, absolute_import, division

import sys
import os
import re
import time
import argparse
import numpy as np

import ctypes as ct
from ctypes.util import find_library

from astropy.io import fits

from desiutil.log import get_logger

import specex as spx
import specter.psf
from . import psfio

modext = "so"
if sys.platform == "darwin":
    modext = "bundle"

specexdata = None

libspecexname = "libspecex.{}".format(modext)
if "LIBSPECEX_DIR" in os.environ:
    libspecexname = os.path.join(os.environ["LIBSPECEX_DIR"],
        "libspecex.{}".format(modext))
    specexdata = os.path.join(
        os.path.dirname(os.environ["LIBSPECEX_DIR"]), "data"
    )
elif "SPECEX" in os.environ:
    specexdata = os.path.join(os.environ["SPECEX"], "data")

libspecex = None
try:
    libspecex = ct.CDLL(libspecexname)
except:
    path = find_library("specex")
    if path is not None:
        libspecex = ct.CDLL(path)
        specexdata = os.path.join(
            os.path.dirname(os.path.dirname(path)), "data"
        )

if libspecex is not None:
    libspecex.cspecex_desi_psf_fit.restype = ct.c_int
    libspecex.cspecex_desi_psf_fit.argtypes = [
        ct.c_int,
        ct.POINTER(ct.POINTER(ct.c_char))
    ]
    libspecex.cspecex_psf_merge.restype = ct.c_int
    libspecex.cspecex_psf_merge.argtypes = [
        ct.c_int,
        ct.POINTER(ct.POINTER(ct.c_char))
    ]
    libspecex.cspecex_spot_merge.restype = ct.c_int
    libspecex.cspecex_spot_merge.argtypes = [
        ct.c_int,
        ct.POINTER(ct.POINTER(ct.c_char))
    ]

#########################################
# TEMPORARY SPECEX-DESISPEC I/O FUNCTIONS

def compare_psfs(specter_psf, specexpy_psf, specex_psf):
    print('specter nspec',specter_psf.nspec)
    print('specex deg   ',specex_psf.Degree())
    return

def meta2header(meta):
    header = spx.MapStringString()

    for key in meta:
        mkey = meta[key]
        if type(mkey) == bool:
            header[key]='F'
            if mkey: header[key]='T'
        elif type(mkey) == str: 
            mstr = mkey
            if len(mstr) < 8: mstr = mstr.ljust(8, ' ')
            header[key]="\'"+mstr+"\'"
        else:
            mstr = str(mkey)
            if len(mstr) > 1:
                if mstr[-2:]=='.0': mstr=mstr[:-1]
            header[key]=mstr

    return header

def read_desi_ppimage_spx(opts):
    import desispec.io.image

    # read images from fits file
    dsmg = desispec.io.image.read_image(opts.arc_image_filename)

    # set ivar=0 to pixels with mask!=0
    dsmg.ivar[dsmg.mask!=0] = 0.0

    # convert from astropy.io.fits.header.Header dict-like object to
    # std::map<std::string,std::string> object via meta2header
    # function above, changing formatting to match current use in
    # specex
    hdr  = meta2header(dsmg.meta)

    # instantiate new specex::PyImage object with arrays and header
    # from the preproc fits file. this object will be passed to the
    # specex::PyFitting::fit_psf routine 
    pymg = spx.PyImage(dsmg.pix, dsmg.ivar,
                       dsmg.mask,dsmg.readnoise,
                       hdr)

    return pymg

def compheaders(header1,header2):
    if len(header1) != len(header2):
        print('lengths not same')
        return 1

    for key in header1:
        if header1[key] != header2[key]:
            print('values for key ',key,' are ',header1[key],' ',header2[key])
            return 1

    return 0

def comparrs(arr1,arr2,tag):

    if np.array_equal(arr1,arr2): return 0
    
    if len(arr1) != len(arr2):
        print(tag,' array length are different')
        return 1
    
    diff = np.abs(arr1-arr2)
    print(tag,' arrays are different')
    print('  min diff',diff.min())
    print('  max diff',diff.max())
    print('  avg diff',diff.mean())
    print('  arr1 avg',arr1.mean())
    print('  arr2 avg',arr2.mean())
    print(np.shape(arr1))
    print(len(diff[diff>0]))
    print(arr1[diff>0],'\n')
    print(arr2[diff>0],'\n')

    return 1

# END TEMPORARY I/O SUPPORT FUNCTIONS
#########################################

def parse(options=None):
    parser = argparse.ArgumentParser(description="Estimate the PSF for "
        "one frame with specex")
    parser.add_argument("--input-image", type=str, required=True,
                        help="input image")
    parser.add_argument("--input-psf", type=str, required=False,
                        help="input psf file")
    parser.add_argument("-o", "--output-psf", type=str, required=True,
                        help="output psf file")
    parser.add_argument("--bundlesize", type=int, required=False, default=25,
                        help="number of spectra per bundle")
    parser.add_argument("-s", "--specmin", type=int, required=False, default=0,
                        help="first spectrum to extract")
    parser.add_argument("-n", "--nspec", type=int, required=False, default=500,
                        help="number of spectra to extract")
    parser.add_argument("--extra", type=str, required=False, default=None,
                        help="quoted string of arbitrary options to pass to "
                        "specex_desi_psf_fit")
    parser.add_argument("--debug", action = 'store_true',
                        help="debug mode")
    parser.add_argument("--broken-fibers", type=str, required=False, default=None,
                        help="comma separated list of broken fibers")
    parser.add_argument("--disable-merge", action = 'store_true',
                        help="disable merging fiber bundles")
    
    args = None
    if options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(options)
    return args


def main(args, comm=None):

    #pyps = opts= 0
    #psfio.write_psf(pyps,opts)
    #return

    log = get_logger()

    imgfile = args.input_image
    outfile = args.output_psf

    if args.input_psf is not None:
        inpsffile = args.input_psf
    else:
        from desispec.calibfinder import findcalibfile
        hdr = fits.getheader(imgfile)
        inpsffile = findcalibfile([hdr,], 'PSF')

    optarray = []
    if args.extra is not None:
        optarray = args.extra.split()

    specmin = int(args.specmin)
    nspec = int(args.nspec)
    bundlesize = int(args.bundlesize)
    specmax = specmin + nspec

    # Now we divide our spectra into bundles

    checkbundles = set()
    checkbundles.update(np.floor_divide(np.arange(specmin, specmax),
        bundlesize*np.ones(nspec)).astype(int))
    bundles = sorted(checkbundles)
    nbundle = len(bundles)

    bspecmin = {}
    bnspec = {}
    for b in bundles:
        if specmin > b * bundlesize:
            bspecmin[b] = specmin
        else:
            bspecmin[b] = b * bundlesize
        if (b+1) * bundlesize > specmax:
            bnspec[b] = specmax - bspecmin[b]
        else:
            bnspec[b] = (b+1) * bundlesize - bspecmin[b]

    # Now we assign bundles to processes

    nproc = 1
    rank = 0
    if comm is not None:
        nproc = comm.size
        rank = comm.rank

    mynbundle = int(nbundle / nproc)
    myfirstbundle = 0
    leftover = nbundle % nproc
    if rank < leftover:
        mynbundle += 1
        myfirstbundle = rank * mynbundle
    else:
        myfirstbundle = ((mynbundle + 1) * leftover) + \
            (mynbundle * (rank - leftover))

    if rank == 0:
        # Print parameters
        log.info("specex:  io_refactor")
        time.sleep(5)
        log.info("specex:  using {} processes".format(nproc))
        log.info("specex:  input image = {}".format(imgfile))
        log.info("specex:  input PSF = {}".format(inpsffile))
        log.info("specex:  output = {}".format(outfile))
        log.info("specex:  bundlesize = {}".format(bundlesize))
        log.info("specex:  specmin = {}".format(specmin))
        log.info("specex:  specmax = {}".format(specmax))
        if args.broken_fibers :
            log.info("specex:  broken fibers = {}".format(args.broken_fibers))

    # get the root output file

    outpat = re.compile(r'(.*)\.fits')
    outmat = outpat.match(outfile)
    if outmat is None:
        raise RuntimeError("specex output file should have .fits extension")
    outroot = outmat.group(1)

    outdir = os.path.dirname(outroot)
    if rank == 0:
        if not os.path.isdir(outdir):
            os.makedirs(outdir)

    failcount = 0

    for b in range(myfirstbundle, myfirstbundle+mynbundle):
        outbundle = "{}_{:02d}".format(outroot, b)
        outbundlefits = "{}.fits".format(outbundle)
        com = ['desi_psf_fit']
        com.extend(['-a', imgfile])
        com.extend(['--in-psf', inpsffile])
        com.extend(['--out-psf', outbundlefits])
        com.extend(['--first-bundle', "{}".format(b)])
        com.extend(['--last-bundle', "{}".format(b)])
        com.extend(['--first-fiber', "{}".format(bspecmin[b])])
        com.extend(['--last-fiber', "{}".format(bspecmin[b]+bnspec[b]-1)])
        if args.broken_fibers :
            com.extend(['--broken-fibers', "{}".format(args.broken_fibers)])
        if args.debug :
            com.extend(['--debug'])

        com.extend(optarray)

        log.debug("proc {} calling {}".format(rank, " ".join(com)))

        # old way 

        # argc = len(com)
        # arg_buffers = [ct.create_string_buffer(com[i].encode('ascii')) \
        #     for i in range(argc)]
        # addrlist = [ ct.cast(x, ct.POINTER(ct.c_char)) for x in \
        #     map(ct.addressof, arg_buffers) ]
        # arg_pointers = (ct.POINTER(ct.c_char) * argc)(*addrlist)        
        # rval = libspecex.cspecex_desi_psf_fit(argc, arg_pointers)

        # new way

        # instantiate specex c++ objects exposed to python        
        opts = spx.PyOptions() # input options
        pyio = spx.PyIO()      # IO options and methods
        pypr = spx.PyPrior()   # Gaussian priors
        pyps = spx.PyPSF()     # psf data
        pyft = spx.PyFitting() # psf fitting

        # copy com to opaque pybind VectorString object args
        spxargs = spx.VectorString()
        for strs in com:
            spxargs.append(strs)
        
        opts.parse(spxargs)                    # parse args
        pyio.check_input_psf(opts)             # set input psf bools
        pypr.deal_with_priors(opts)            # set Gaussian priors
        
        pymg = read_desi_ppimage_spx(opts)     # read preproc images (desispec)        
        pyio.read_psf_data(opts,pyps)          # read psf (specex)        

        pyft.fit_psf(opts,pyio,pypr,pymg,pyps) # fit psf (specex)

        pyio.prepare_psf(opts,pyps)            # prepare psf (specex)
        psfio.write_psf(pyps,opts)             # write psf (fitsio)
        pyio.write_spots(opts,pyps)            # write spots

    if comm is not None:
        from mpi4py import MPI
        failcount = comm.allreduce(failcount, op=MPI.SUM)

    if failcount > 0:
        # all processes throw
        raise RuntimeError("some bundles failed desi_psf_fit")

    if rank == 0:
        outfits = "{}.fits".format(outroot)

        inputs = [ "{}_{:02d}.fits".format(outroot, x) for x in bundles ]

        args.disable_merge=False
        if args.disable_merge :
            log.info("don't merge")
        else :
            #- Empirically it appears that files written by one rank sometimes
            #- aren't fully buffer-flushed and closed before getting here,
            #- despite the MPI allreduce barrier.  Pause to let I/O catch up.
            log.info('HACK: taking a 20 sec pause before merging')
            sys.stdout.flush()
            time.sleep(20.)

            merge_psf(inputs,outfits)

            log.info('done merging')

            if failcount == 0:
                # only remove the per-bundle files if the merge was good
                for f in inputs :
                    if os.path.isfile(f):
                        os.remove(f)

    if comm is not None:
        failcount = comm.bcast(failcount, root=0)

    if failcount > 0:
        # all processes throw
        raise RuntimeError("merging of per-bundle files failed")

    return


def compatible(head1, head2) :
    log = get_logger()
    for k in ["PSFTYPE", "NPIX_X", "NPIX_Y", "HSIZEX", "HSIZEY", "FIBERMIN",
        "FIBERMAX", "NPARAMS", "LEGDEG", "GHDEGX", "GHDEGY"] :
        if (head1[k] != head2[k]) :
            log.warning("different {} : {}, {}".format(k, head1[k], head2[k]))
            return False
    return True


def merge_psf(inputs, output):

    log = get_logger()

    npsf = len(inputs)
    log.info("Will merge {} PSFs in {}".format(npsf,output))

    # we will add/change data to the first PSF
    psf_hdulist=fits.open(inputs[0])
    for input_filename in inputs[1:] :
        log.info("merging {} into {}".format(input_filename,inputs[0]))
        other_psf_hdulist=fits.open(input_filename)

        # look at what fibers where actually fit
        i=np.where(other_psf_hdulist["PSF"].data["PARAM"]=="STATUS")[0][0]
        status_of_fibers = \
            other_psf_hdulist["PSF"].data["COEFF"][i][:,0].astype(int)
        selected_fibers = np.where(status_of_fibers==0)[0]
        log.info("fitted fibers in PSF {} = {}".format(input_filename,
            selected_fibers))
        if selected_fibers.size == 0 :
            log.warning("no fiber with status=0 found in {}".format(
                input_filename))
            other_psf_hdulist.close()
            continue

        # copy xtrace and ytrace
        psf_hdulist["XTRACE"].data[selected_fibers] = \
            other_psf_hdulist["XTRACE"].data[selected_fibers]
        psf_hdulist["YTRACE"].data[selected_fibers] = \
            other_psf_hdulist["YTRACE"].data[selected_fibers]

        # copy parameters
        parameters = psf_hdulist["PSF"].data["PARAM"]
        for param in parameters :
            i0=np.where(psf_hdulist["PSF"].data["PARAM"]==param)[0][0]
            i1=np.where(other_psf_hdulist["PSF"].data["PARAM"]==param)[0][0]
            psf_hdulist["PSF"].data["COEFF"][i0][selected_fibers] = \
                other_psf_hdulist["PSF"].data["COEFF"][i1][selected_fibers]

        # copy bundle chi2
        i = np.where(other_psf_hdulist["PSF"].data["PARAM"]=="BUNDLE")[0][0]
        bundles = np.unique(other_psf_hdulist["PSF"].data["COEFF"][i]\
            [selected_fibers,0].astype(int))
        log.info("fitted bundles in PSF {} = {}".format(input_filename,
            bundles))
        for b in bundles :
            for key in [ "B{:02d}RCHI2".format(b), "B{:02d}NDATA".format(b),
                "B{:02d}NPAR".format(b) ]:
                psf_hdulist["PSF"].header[key] = \
                    other_psf_hdulist["PSF"].header[key]
        # close file
        other_psf_hdulist.close()

    # write
    psf_hdulist.writeto(output,overwrite=True)
    log.info("Wrote PSF {}".format(output))

    return


def mean_psf(inputs, output):

    log = get_logger()

    npsf = len(inputs)
    log.info("Will compute the average of {} PSFs".format(npsf))

    refhead=None
    tables=[]
    xtrace=[]
    ytrace=[]
    wavemins=[]
    wavemaxs=[]

    hdulist=None
    bundle_rchi2=[]
    nbundles=None
    nfibers_per_bundle=None

    
    for input in inputs :
        log.info("Adding {}".format(input))
        if not os.path.isfile(input) :
            log.warning("missing {}".format(input))
            continue
        psf=fits.open(input)
        if refhead is None :
            hdulist = psf
            refhead = psf["PSF"].header
            nfibers = \
                (psf["PSF"].header["FIBERMAX"]-psf["PSF"].header["FIBERMIN"])+1
            PSFVER=int(refhead["PSFVER"])
            if(PSFVER<3) :
                log.error("ERROR NEED PSFVER>=3")
                sys.exit(1)

        else :
            if not compatible(psf["PSF"].header,refhead) :
                log.error("psfs {} and {} are not compatible".format(inputs[0],
                    input))
                sys.exit(12)
        tables.append(psf["PSF"].data)
        wavemins.append(psf["PSF"].header["WAVEMIN"])
        wavemaxs.append(psf["PSF"].header["WAVEMAX"])

        if "XTRACE" in psf :
            xtrace.append(psf["XTRACE"].data)
        if "YTRACE" in psf :
            ytrace.append(psf["YTRACE"].data)

        rchi2=[]
        b=0
        while "B{:02d}RCHI2".format(b) in psf["PSF"].header :
            rchi2.append(psf["PSF"].header["B{:02d}RCHI2".format(b) ])
            b += 1
        rchi2=np.array(rchi2)
        nbundles=rchi2.size
        bundle_rchi2.append(rchi2)

    npsf=len(tables)
    bundle_rchi2=np.array(bundle_rchi2)
    log.debug("bundle_rchi2= {}".format(str(bundle_rchi2)))
    median_bundle_rchi2 = np.median(bundle_rchi2)
    rchi2_threshold=median_bundle_rchi2+1.
    log.debug("median chi2={} threshold={}".format(median_bundle_rchi2,
        rchi2_threshold))

    WAVEMIN=refhead["WAVEMIN"]
    WAVEMAX=refhead["WAVEMAX"]
    FIBERMIN=int(refhead["FIBERMIN"])
    FIBERMAX=int(refhead["FIBERMAX"])


    fibers_in_bundle={}
    i=np.where(tables[0]["PARAM"]=="BUNDLE")[0][0]
    bundle_of_fibers=tables[0]["COEFF"][i][:,0].astype(int)
    bundles=np.unique(bundle_of_fibers)
    for b in bundles :
        fibers_in_bundle[b]=np.where(bundle_of_fibers==b)[0]

    for entry in range(tables[0].size) :
        PARAM=tables[0][entry]["PARAM"]
        log.info("Averaging '{}' coefficients".format(PARAM))
        coeff=[tables[0][entry]["COEFF"]]
        npar=coeff[0][1].size
        for p in range(1,npsf) :

            if wavemins[p]==WAVEMIN and wavemaxs[p]==WAVEMAX :
                coeff.append(tables[p][entry]["COEFF"])
            else :
                log.info("need to refit legendre polynomial ...")
                icoeff = tables[p][entry]["COEFF"]
                ocoeff = np.zeros(icoeff.shape)
                # need to reshape legpol
                iu = np.linspace(-1,1,npar+3)
                iwavemin = wavemins[p]
                iwavemax = wavemaxs[p]
                wave = (iu+1.)/2.*(iwavemax-iwavemin)+iwavemin
                ou = (wave-WAVEMIN)/(WAVEMAX-WAVEMIN)*2.-1.
                for f in range(icoeff.shape[0]) :
                    val = legval(iu,icoeff[f])
                    ocoeff[f] = legfit(ou,val,deg=npar-1)
                coeff.append(ocoeff)

        coeff=np.array(coeff)

        output_rchi2=np.zeros((bundle_rchi2.shape[1]))
        output_coeff=np.zeros(tables[0][entry]["COEFF"].shape)

        # now merge, using rchi2 as selection score

        for bundle in fibers_in_bundle.keys() :

            ok=np.where(bundle_rchi2[:,bundle]<rchi2_threshold)[0]
            #ok=np.array([0,1]) # debug

            if entry==0 :
                log.info("for fiber bundle {}, {} valid PSFs".format(bundle,
                    ok.size))

            if ok.size>=2 : # use median
                log.debug("bundle #{} : use median".format(bundle))
                for f in fibers_in_bundle[bundle]  :
                    output_coeff[f]=np.median(coeff[ok,f],axis=0)
                output_rchi2[bundle]=np.median(bundle_rchi2[ok,bundle])
            elif ok.size==1 : # copy
                log.debug("bundle #{} : use only one psf ".format(bundle))
                for f in fibers_in_bundle[bundle]  :
                    output_coeff[f]=coeff[ok[0],f]
                output_rchi2[bundle]=bundle_rchi2[ok[0],bundle]

            else : # we have a problem here, take the smallest rchi2
                log.debug("bundle #{} : take smallest chi2 ".format(bundle))
                i=np.argmin(bundle_rchi2[:,bundle])
                for f in fibers_in_bundle[bundle]  :
                    output_coeff[f]=coeff[i,f]
                output_rchi2[bundle]=bundle_rchi2[i,bundle]

        # now copy this in output table
        hdulist["PSF"].data["COEFF"][entry]=output_coeff
        # change bundle chi2
        for bundle in range(output_rchi2.size) :
            hdulist["PSF"].header["B{:02d}RCHI2".format(bundle)] = \
                output_rchi2[bundle]

        if len(xtrace)>0 :
            xtrace=np.array(xtrace)
            ytrace=np.array(ytrace)
            for p in range(xtrace.shape[0]) :
                if wavemins[p]==WAVEMIN and wavemaxs[p]==WAVEMAX :
                    continue

                # need to reshape legpol
                iu = np.linspace(-1,1,npar+3)
                iwavemin = wavemins[p]
                iwavemax = wavemaxs[p]
                wave = (iu+1.)/2.*(iwavemax-iwavemin)+iwavemin
                ou = (wave-WAVEMIN)/(WAVEMAX-WAVEMIN)*2.-1.

                for f in range(icoeff.shape[0]) :
                    val = legval(iu,xtrace[f])
                    xtrace[f] = legfit(ou,val,deg=npar-1)
                    val = legval(iu,ytrace[f])
                    ytrace[f] = legfit(ou,val,deg=npar-1)

            hdulist["xtrace"].data = np.median(np.array(xtrace),axis=0)
            hdulist["ytrace"].data = np.median(np.array(ytrace),axis=0)

        # alter other keys in header
        hdulist["PSF"].header["EXPID"]=0. # it's a mix, need to add the expids

    for hdu in ["XTRACE","YTRACE","PSF"] :
        if hdu in hdulist :
            for input in inputs :
                hdulist[hdu].header["comment"] = "inc {}".format(input)
        
    # save output PSF
    hdulist.writeto(output, overwrite=True)
    log.info("wrote {}".format(output))

    return
