import logging
import warnings

from scipy.signal import medfilt

from stdatamodels.jwst import datamodels

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


def apply_master_background(source_model, bkg_model, inverse=False):
    """Subtract 2D master background signal from each source
    slitlet in the input MultiSlitModel.

    Parameters
    ----------
    source_model : `~jwst.datamodels.MultiSlitModel`
        The input data model containing all source slit instances.

    bkg_model : `~jwst.datamodels.MultiSlitModel`
        The data model containing 2D background slit instances.

    inverse : boolean
        Invert the math operations used to apply the background.

    Returns
    -------
    output_model: `~jwst.datamodels.MultiSlitModel`
        The output background-subtracted data model.
    """
    from .master_background_step import subtract_2d_background

    if inverse:
        log.info('Adding master background from each MOS source slitlet')
        bkg = bkg_model.copy()
        for slit in bkg.slits:
            slit.data *= -1.0
    else:
        log.info('Subtracting master background from each MOS source slitlet')
        bkg = bkg_model

    # This does a one-to-one subtraction of the data in each background
    # slit from the data in the corresponding source slit (i.e. the
    # two MultiSlitModels must have matching numbers of slit instances).
    # This may be changed in the future to only do the subtraction from
    # a certain subset of source slits.
    output_model = subtract_2d_background(source_model, bkg)

    return output_model


def map_to_science_slits(input_model, master_bkg):
    """Interpolate 1D master background spectrum to the 2D space
    of each source slitlet in the input MultiSlitModel.

    Parameters
    ----------
    input_model : `~jwst.datamodels.MultiSlitModel`
        The input data model containing all slit instances.

    master_bkg : `~jwst.datamodels.CombinedSpecModel`
        The 1D master background spectrum.

    Returns
    -------
    output_model: `~jwst.datamodels.MultiSlitModel`
        The output data model containing background signal.
    """
    from .expand_to_2d import expand_to_2d

    log.info('Interpolating 1D master background to all MOS 2D slitlets')

    # Loop over all input slits, creating 2D master background to
    # match each 2D slitlet cutout
    output_model = expand_to_2d(input_model, master_bkg, allow_mos=True)

    return output_model


def create_background_from_multislit(input_model, sigma_clip=3, median_kernel=1):
    """Create a 1D master background spectrum from a set of
    calibrated background MOS slitlets in the input
    MultiSlitModel.

    Parameters
    ----------
    input_model : `~jwst.datamodels.MultiSlitModel`
        The input data model containing all slit instances.
    sigma_clip : None or float, optional
        Optional factor for sigma clipping outliers when combining background spectra.
    median_kernel : integer, optional
        Optional user-supplied kernel with which to moving-median boxcar filter the master background
        spectrum.  Must be an odd integer, even integers will be rounded down to the nearest
        odd integer.

    Returns
    -------
    master_bkg: `~jwst.datamodels.CombinedSpecModel`
        The 1D master background spectrum created from the inputs.
    x1d: `jwst.datamodels.MultiSpecModel`
        The 1D extracted background spectra of the inputs.
    """
    from ..resample import resample_spec_step
    from ..extract_1d import extract_1d_step
    from ..combine_1d.combine1d import combine_1d_spectra

    log.info('Creating MOS master background from background slitlets')

    # Copy dedicated background slitlets to a temporary model
    bkg_model = datamodels.MultiSlitModel()
    bkg_model.update(input_model)
    slits = []
    for slit in input_model.slits:
        if is_background_msa_slit(slit):
            log.info(f'Using background slitlet {slit.source_name}')
            slits.append(slit)

    if len(slits) == 0:
        log.warning('No background slitlets found; skipping master bkg correction')
        return None

    bkg_model.slits.extend(slits)

    # Apply resample_spec and extract_1d to all background slitlets
    log.info('Applying resampling and 1D extraction to background slits')
    resamp = resample_spec_step.ResampleSpecStep.call(bkg_model)
    x1d = extract_1d_step.Extract1dStep.call(resamp)

    # Call combine_1d to combine the 1D background spectra
    log.info('Combining 1D background spectra into master background')
    master_bkg = combine_1d_spectra(
        x1d, exptime_key='exposure_time', sigma_clip=sigma_clip)

    # If requested, apply a moving-median boxcar filter to the master background spectrum
    # Round down even kernel sizes because only odd kernel sizes are supported.
    if median_kernel % 2 == 0:
        median_kernel -= 1
        log.info('Even median filter kernels are not supported.'
                        f' Rounding the median kernel size down to {median_kernel}.')

    if (median_kernel > 1):
        log.info(f'Applying moving-median boxcar of width {median_kernel}.')
        master_bkg.spec[0].spec_table['surf_bright'] = medfilt(
            master_bkg.spec[0].spec_table['surf_bright'],
            kernel_size=[median_kernel]
        )
        master_bkg.spec[0].spec_table['flux'] = medfilt(
            master_bkg.spec[0].spec_table['flux'],
            kernel_size=[median_kernel]
        )

    del bkg_model
    del resamp

    return master_bkg, x1d


def correct_nrs_ifu_bkg(input_model):
    """Apply point source vs. uniform source pathloss adjustments
    to a NIRSpec IFU 2D master background array.

    Parameters
    ----------
    input_model : `~jwst.datamodels.IFUImageModel`
        The input background data.

    Returns
    -------
    input_model : `~jwst.datamodels.IFUIMAGEModel`
        An updated (in place) version of the input with the data
        replaced by the corrected 2D background.
    """

    log.info('Applying point source pathloss updates to IFU background')

    # Try to load the appropriate pathloss correction arrays
    try:
        pl_point = input_model.getarray_noinit('pathloss_point')
    except AttributeError:
        log.warning('Pathloss_point array not found in input')
        log.warning('Skipping pathloss background updates')
        return input_model

    try:
        pl_uniform = input_model.getarray_noinit('pathloss_uniform')
    except AttributeError:
        log.warning('Pathloss_uniform array not found in input')
        log.warning('Skipping pathloss background updates')
        return input_model

    # Apply the corrections
    input_model.data *= (pl_uniform / pl_point)

    return input_model


def correct_nrs_fs_bkg(input_model):
    """Apply point source vs. uniform source corrections
    to a NIRSpec Fixed-Slit 2D master background array.

    Parameters
    ----------
    input_model : `~jwst.datamodels.SlitModel`
        The input background data.

    Returns
    -------
    input_model : `~jwst.datamodels.SlitModel`
        An updated (in place) version of the input with the data
        replaced by the corrected 2D background.
    """
    log.info('Applying point source updates to FS background')

    # Try to load the appropriate pathloss correction arrays
    if 'pathloss_point' in input_model.instance:
        pl_point = getattr(input_model, 'pathloss_point')
    else:
        log.warning('pathloss_point array not found in input')
        log.warning('Skipping background updates')
        return input_model

    if 'pathloss_uniform' in input_model.instance:
        pl_uniform = getattr(input_model, 'pathloss_uniform')
    else:
        log.warning('pathloss_uniform array not found in input')
        log.warning('Skipping background updates')
        return input_model

    # If processing the primary slit, we also need flatfield and
    # photom correction arrays
    if 'flatfield_point' in input_model.instance:
        ff_point = getattr(input_model, 'flatfield_point')
    else:
        log.warning('flatfield_point array not found in input')
        log.warning('Skipping background updates')
        return input_model

    if 'flatfield_uniform' in input_model.instance:
        ff_uniform = getattr(input_model, 'flatfield_uniform')
    else:
        log.warning('flatfield_uniform array not found in input')
        log.warning('Skipping background updates')
        return input_model

    if 'photom_point' in input_model.instance:
        ph_point = getattr(input_model, 'photom_point')
    else:
        log.warning('photom_point array not found in input')
        log.warning('Skipping background updates')
        return input_model

    if 'photom_uniform' in input_model.instance:
        ph_uniform = getattr(input_model, 'photom_uniform')
    else:
        log.warning('photom_uniform array not found in input')
        log.warning('Skipping background updates')
        return input_model

    # Apply the corrections for the primary slit
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "invalid value*", RuntimeWarning)
        warnings.filterwarnings("ignore", "divide by zero*", RuntimeWarning)
        input_model.data *= (pl_uniform / pl_point) * \
                            (ff_uniform / ff_point) * \
                            (ph_point / ph_uniform)

    return input_model


def is_background_msa_slit(slit):
    """
    Check if an MSA slitlet is a background source.

    Parameters
    ----------
    slit : `~jwst.datamodels.SlitModel`
        The slit model to check.

    Returns
    -------
    bool
        True if the slit is background; False if it is not.
    """
    name = str(slit.source_name).upper()
    if ("BKG" in name) or ("BACKGROUND" in name):
        return True
    else:
        return False
