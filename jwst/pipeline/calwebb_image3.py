from stdatamodels.jwst import datamodels

from jwst.datamodels.library import ModelLibrary

from ..stpipe import Pipeline
from ..lib.exposure_types import is_moving_target

from ..assign_mtwcs import assign_mtwcs_step
from ..tweakreg import tweakreg_step
from ..skymatch import skymatch_step
from ..resample import resample_step
from ..outlier_detection import outlier_detection_step
from ..source_catalog import source_catalog_step

__all__ = ['Image3Pipeline']


class Image3Pipeline(Pipeline):
    """
    Image3Pipeline: Applies level 3 processing to imaging-mode data from
                    any JWST instrument.

    Included steps are:
        assign_mtwcs
        tweakreg
        skymatch
        outlier_detection
        resample
        source_catalog
    """

    class_alias = "calwebb_image3"

    spec = """
    """

    # Define alias to steps
    step_defs = {
        'assign_mtwcs': assign_mtwcs_step.AssignMTWcsStep,
        'tweakreg': tweakreg_step.TweakRegStep,
        'skymatch': skymatch_step.SkyMatchStep,
        'outlier_detection': outlier_detection_step.OutlierDetectionStep,
        'resample': resample_step.ResampleStep,
        'source_catalog': source_catalog_step.SourceCatalogStep
    }

    def process(self, input_data):
        """
        Run the Image3Pipeline

        Parameters
        ----------
        input_data: Level3 Association
            The exposures to process
        """
        # TODO check that this is an association?
        # TODO convert from list of models or ModelContainer
        assert isinstance(input_data, str)
        self.log.info('Starting calwebb_image3 ...')

        # Only load science members from input ASN;
        # background and target-acq members are not needed.
        asn_exptypes = ['science']

        # Configure settings for saving results files
        self.outlier_detection.suffix = 'crf'
        self.outlier_detection.save_results = self.save_results

        self.resample.suffix = 'i2d'
        #self.resample.save_results = self.save_results
        # FIXME is there a way to override save_results for one step in a pipeline?
        self.resample.save_results = True

        self.source_catalog.save_results = self.save_results

        # TODO add option for on_disk
        library = ModelLibrary(input_data, asn_exptypes=asn_exptypes, on_disk=True)
        self.outlier_detection.in_memory = False
        self.resample.in_memory = False

        # If input is an association, set the output to the product name.
        if self.output_file is None:
            try:
                # TODO update with API for _asn
                self.output_file = library._asn['products'][0]['name']
            except (AttributeError, IndexError):
                pass

        # Check if input is single or multiple exposures
        try:
            has_groups = len(library.group_names) >= 1
        except (AttributeError, TypeError, KeyError):
            has_groups = False

        if has_groups:
            with library:
                model = library[0]
                # FIXME why does is_moving_target expect a list/container?
                moving_target = is_moving_target([model])
                library[0] = model
            if moving_target:
                # TODO update assign_mtwcs
                raise NotImplementedError()
                library = self.assign_mtwcs(library)
            else:
                library = self.tweakreg(library)

            library = self.skymatch(library)
            library = self.outlier_detection(library)

        elif self.skymatch.skymethod == 'match':
            self.log.warning("Turning 'skymatch' step off for a single "
                             "input image when 'skymethod' is 'match'")

        else:
            library = self.skymatch(library)

        result = self.resample(library)
        if isinstance(result, datamodels.ImageModel) and result.meta.cal_step.resample == 'COMPLETE':
            self.source_catalog(result)

        return result
