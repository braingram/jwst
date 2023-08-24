
import astropy.io.fits as fits
import matplotlib.pyplot as plt
import numpy as np

from scipy import stats

import argparse
import glob
import os
import time

from copy import deepcopy
from poppy import matrixDFT
from scipy.ndimage import median_filter

from stdatamodels.jwst.datamodels import dqflags


"""pipeline implementation of Jens Kammerer's bp_fix code based on Ireland 2013 algorithm"""

micron = 1.0e-6
filts = ['F277W', 'F380M', 'F430M', 'F480M', 'F356W', 'F444W']
filtwl_d = {  # pivot wavelengths
	'F277W': 2.776e-6,  # less than Nyquist
	'F380M': 3.828e-6,
	'F430M': 4.286e-6,
	'F480M': 4.817e-6,
	'F356W': 3.595e-6,  # semi-forbidden
	'F444W': 4.435e-6,  # semi-forbidden
}
filthp_d = {  # half power limits
	'F277W': (2.413e-6, 3.142e-6),
	'F380M': (3.726e-6, 3.931e-6),
	'F430M': (4.182e-6, 4.395e-6),
	'F480M': (4.669e-6, 4.971e-6),
	'F356W': (3.141e-6, 4.068e-6),
	'F444W': (3.880e-6, 5.023e-6),
}
WL_OVERSIZEFACTOR = 0.1  # increase filter wl support by this amount to 'oversize' in wl space

pix_arcsec = 0.0656  # nominal isotropic pixel scale - refine later
pix_rad = pix_arcsec * np.pi / (60 * 60 * 180)

# GET PUPIL MASK FROM WEBBPSF
webbpsf_path = os.getenv('WEBBPSF_PATH')
pupilfile_nrm = os.path.join(webbpsf_path,'NIRISS/optics/MASK_NRM.fits.gz')
nrm_pupil = fits.getdata(pupilfile_nrm)

#clearp_pupil = fits.getdata(pupilfile_clearp)
pupil_masks = {
	"NRM": nrm_pupil,
#    "CLEARP": clearp_pupil,
}

DIAM = 6.559348  # / Flat-to-flat distance across pupil in V3 axis
PUPLDIAM = 6.603464  # / Full pupil file size, incl padding.
PUPL_CRC = 6.603464  # / Circumscribing diameter for JWST primary

def create_wavelengths(filtername):
	"""
	filtername str: filter name
	Extend filter support slightly past half power points.
	Filter transmissions are quasi-rectangular.
	"""
	wl_ctr = filtwl_d[filtername]
	wl_hps = filthp_d[filtername]
	# both positive quantities below - left is lower wl, rite is higher wl
	dleft = (wl_ctr - wl_hps[0]) * (1 + WL_OVERSIZEFACTOR)
	drite = (-wl_ctr + wl_hps[1]) * (1 + WL_OVERSIZEFACTOR)

	return (wl_ctr, wl_ctr - dleft, wl_ctr + drite)

def calcsupport(filtername, sqfov_npix, pupil="NRM"):
	"""
	filtername str: filter name
	calculate psf at low center high wavelengths of filter
	coadd psfs
	perform fft-style transform of image w/dft
	send back absolute value of FT(image) in filter - the CV Vsq array
	"""
	wls = create_wavelengths(filtername)
	print(f"      {filtername}: {wls[0] / micron:.3f} to {wls[2] / micron:.3f} micron")
	detimage = np.zeros((sqfov_npix, sqfov_npix), float)
	for wl in wls:
		psf = calcpsf(wl, sqfov_npix, pupil=pupil)
		detimage += psf

	return transform_image(detimage)


def transform_image(image):
	ft = matrixDFT.MatrixFourierTransform()
	ftimage = ft.perform(image, image.shape[0], image.shape[0])  # fake the no-loss fft w/ dft

	return np.abs(ftimage)

def calcpsf(wl, fovnpix, pupil="NRM"):
	"""
	input wl: float meters wavelength
	input fovnpix: feld of view (square) in number of pixels
	returns monochromatic unnormalized psf
	"""
	reselt = wl / PUPLDIAM  # radian
	nlamD = fovnpix * pix_rad / reselt  # Soummer nlamD FOV in reselts
	# instantiate an mft object:
	ft = matrixDFT.MatrixFourierTransform()

	pupil_mask = pupil_masks[pupil]
	image_field = ft.perform(pupil_mask, nlamD, fovnpix)
	image_intensity = (image_field * image_field.conj()).real

	return image_intensity

def bad_pixels(data,
			   median_size,
			   median_tres):
	"""
	Identify bad pixels by subtracting median-filtered data and searching for
	outliers.
	"""

	mfil_data = median_filter(data, size=median_size)
	diff_data = np.abs(data - mfil_data)
	pxdq = diff_data > median_tres * np.median(diff_data)
	pxdq = pxdq.astype('int')

	print('         Identified %.0f bad pixels (%.2f%%)' % (np.sum(pxdq), np.sum(pxdq) / np.prod(pxdq.shape) * 100.))
	print('         %.3f' % np.max(diff_data/np.median(diff_data)))

	return pxdq


def fourier_corr(data,
				 pxdq,
				 fmas):
	"""
	Compute and apply the bad pixel corrections based on Section 2.5 of
	Ireland 2013. This function is the core of the bad pixel cleaning code.
	"""

	# Get the dimensions.
	ww = np.where(pxdq > 0.5)
	ww_ft = np.where(fmas)

	# Compute the B_Z matrix from Section 2.5 of Ireland 2013. This matrix
	# maps the bad pixels onto their Fourier power in the domain Z, which is
	# the complement of the pupil support.
	B_Z = np.zeros((len(ww[0]), len(ww_ft[0]) * 2))
	xh = data.shape[0] // 2
	yh = data.shape[1] // 2
	xx, yy = np.meshgrid(2. * np.pi * np.arange(yh + 1) / data.shape[1],
						 2. * np.pi * (((np.arange(data.shape[0]) + xh) % data.shape[0]) - xh) / data.shape[0])
	for i in range(len(ww[0])):
		cdft = np.exp(-1j * (ww[0][i] * yy + ww[1][i] * xx))
		B_Z[i, :] = np.append(cdft[ww_ft].real, cdft[ww_ft].imag)

	# Compute the corrections for the bad pixels using the Moore-Penrose pseudo
	# inverse of B_Z (Equation 19 of Ireland 2013).
	B_Z_ct = np.transpose(np.conj(B_Z))
	B_Z_mppinv = np.dot(B_Z_ct, np.linalg.inv(np.dot(B_Z, B_Z_ct)))

	# Apply the corrections for the bad pixels.
	data_out = deepcopy(data)
	data_out[ww] = 0.
	data_ft = np.fft.rfft2(data_out)[ww_ft]
	corr = -np.real(np.dot(np.append(data_ft.real, data_ft.imag), B_Z_mppinv))
	data_out[ww] += corr

	return data_out


def fix_bad_pixels(data, pxdq0, filt):
	"""
	the first thing original run_bp_fix code does is crop the data (roughly) around the psf center,
	and make a mask from the dq array. Then passed to the function that actually does the fourier correction.
	the step also does the cropping, dq-mask-making for fringe fitting. So, bp-fix the data after this is done. currently cropping
	is done of each slice, in fringe fitting. why not do it up front? 
	needs to know filter -- pass in as arg?

	"""
	DO_NOT_USE = dqflags.pixel["DO_NOT_USE"]
	JUMP_DET = dqflags.pixel["JUMP_DET"]
	dq_dnu = pxdq0 & DO_NOT_USE == DO_NOT_USE
	dq_jump = pxdq0 & JUMP_DET == JUMP_DET
	dqmask = dq_dnu | dq_jump

	pxdq = np.where(dqmask, pxdq0, 0)
	nflagged_dnu = np.count_nonzero(pxdq)
	print('%i pixels flagged DO_NOT_USE in cropped data' % nflagged_dnu)

	# DNU, some other pixels are now NaNs in cal level products.
	# Replace them with 0, then
	# add DO_NOT_USE flags to positions in DQ array so they will be corrected.
	nanidxlist = np.argwhere(np.isnan(data))
	if len(nanidxlist) > 1:
		print("Identified %i NaN pixels to correct" % len(nanidxlist))
		for idx in nanidxlist:
			data[idx[0],idx[1],idx[2]] = 0
			pxdq0[idx[0],idx[1],idx[2]] += 1 # add DNU flag to each nan pixel

	# These values are taken from the JDox and the SVO Filter Profile
	# Service.
	diam = PUPLDIAM  # m
	gain = 1.61  # e-/ADU
	rdns = 18.32  # e-
	pxsc = pix_arcsec * 1000.  # mas/pix

	# These values were determined empirically for NIRISS/AMI and need to be
	# tweaked for any other instrument.
	median_size = 3  # pix
	median_tres = 50. # JK: changed from 28 to 20 in order to capture all bad pixels

	pupil = "NRM"
	imsz = data.shape
	sh = imsz[-1] //2 # half size, even
	# Compute field-of-view and Fourier sampling.
	fov = 2 * sh * pxsc / 1000.  # arcsec
	fsam = filtwl_d[filt] / (fov / 3600. / 180. * np.pi)  # m/pix
	print('      FOV = %.1f arcsec, Fourier sampling = %.3f m/pix' % (fov, fsam))

	#
	cvis = calcsupport(filt, 2 * sh, pupil=pupil) # CHECK IF THIS IS CAUSING PROBLEMS
	cvis /= np.max(cvis)
	fmas = cvis < 1e-3  # 1e-3 seems to be a reasonable threshold
	fmas_show = fmas.copy()
	fmas = np.fft.fftshift(fmas)[:, :2 * sh // 2 + 1]

	# Compute the pupil mask. This mask defines the region where we are
	# measuring the noise. It looks like 15 lambda/D distance from the PSF
	# is reasonable.
	ramp = np.arange(2 * sh) - 2 * sh // 2
	xx, yy = np.meshgrid(ramp, ramp)
	dist = np.sqrt(xx ** 2 + yy ** 2)
	if (pupil == 'NRM'):
		pmas = dist > 9. * filtwl_d[filt] / diam * 180. / np.pi * 1000. * 3600. / pxsc
	else:
		pmas = dist > 12. * filtwl_d[filt] / diam * 180. / np.pi * 1000. * 3600. / pxsc
	# if (np.sum(pmas) < np.mean(flagged_per_int)):
	#     print('   SKIPPING: subframe too small to estimate noise')
	#     continue


	# Go through all frames.
	for j in range(imsz[0]):
		print('         Frame %.0f of %.0f' % (j + 1, imsz[0]))

		# Now cut out the subframe.
		# no need to cut out sub-frame; data already cropped
		# odd/even size issues?
		data_cut = deepcopy(data[j,:-1,:-1])
		data_orig = deepcopy(data_cut)
		pxdq_cut = deepcopy(pxdq[j,:-1,:-1])
		pxdq_cut = pxdq_cut > 0.5
		pxdq_orig = deepcopy(pxdq_cut)
		# Correct the bad pixels. This is an iterative process. After each
		# iteration, we check whether new (residual) bad pixels are
		# identified. If so, we re-compute the corrections. If not, we
		# terminate the iteration.		
		for k in range(10):

			# plt.figure()
			# plt.imshow(np.log10(data_cut), origin='lower')
			# plt.imshow(pmas, alpha=0.5, origin='lower')
			# plt.colorbar()
			# plt.show()

			# plt.figure()
			# plt.imshow(np.log10(np.abs(np.fft.rfft2(np.fft.fftshift(data_cut)))), origin='lower')
			# plt.imshow(fmas, alpha=0.5, origin='lower')
			# plt.colorbar()
			# plt.show()

			# Correct the bad pixels.
			data_cut = fourier_corr(data_cut,
									pxdq_cut,
									fmas)
			if (k == 0):
				data_temp = deepcopy(data_cut)

			# Identify residual bad pixels by looking at the high spatial
			# frequency part of the image.
			fmas_data = np.real(np.fft.irfft2(np.fft.rfft2(data_cut) * fmas))

			# Analytically determine the noise (Poisson noise + read noise)
			# and normalize the high spatial frequency part of the image
			# by it, then identify residual bad pixels.
			mfil_data = median_filter(data_cut, size=median_size)
			nois = np.sqrt(mfil_data / gain + rdns ** 2)
			fmas_data /= nois
			temp = bad_pixels(fmas_data,
							  median_size=median_size,
							  median_tres=median_tres)

			# Check which bad pixels are new. Also, compare the
			# analytically determined noise with the empirically measured
			# noise.
			pxdq_new = np.sum(temp[pxdq_cut < 0.5])
			print('         Iteration %.0f: %.0f new bad pixels, sdev of norm noise = %.3f' % (k + 1, pxdq_new, np.std(fmas_data[pmas])))

			# If no new bad pixels were identified, terminate the
			# iteration.
			if (pxdq_new == 0.):
				break

			# If new bad pixels were identified, add them to the bad pixel
			# map.
			pxdq_cut = ((pxdq_cut > 0.5) | (temp > 0.5)).astype('int')


		# Put the modified frames back into the data cube.
		data[j,:-1,:-1] = fourier_corr(data_orig,pxdq_cut,fmas)
		pxdq[j,:-1,:-1] = pxdq_cut

		

	# Save the corrected data into the original FITS file.
	# hdul['SCI'].data = data
	# hdul['DQ'].data = pxdq0 # original dq array
	# hdul.writeto(os.path.join(odir, fitsfiles[i]), output_verify='fix', overwrite=True)
	# hdul.close()

	return data, pxdq 