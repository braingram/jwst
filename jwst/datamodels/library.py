from collections.abc import MutableMapping, Sequence
import io
from pathlib import Path
import os.path
import tempfile
from types import MappingProxyType

import asdf
from astropy.io import fits

from stdatamodels.jwst.datamodels.util import open as datamodels_open

from .container import ModelContainer


class LibraryError(Exception):
    pass


class BorrowError(LibraryError):
    pass


class ClosedLibraryError(LibraryError):
    pass


class _OnDiskModelStore(MutableMapping):
    def __init__(self, memmap=False, directory=None):
        self._memmap = memmap
        if directory is None:
            # when tem
            self._tempdir = tempfile.TemporaryDirectory(dir='')
            # TODO should I make this a path?
            self._dir = self._tempdir.name
        else:
            self._dir = directory
        self._filenames = {}

    def __getitem__(self, key):
        if key not in self._filenames:
            raise KeyError(f"{key} is not in {self}")
        return datamodels_open(self._filenames[key], memmap=self._memmap)

    def __setitem__(self, key, value):
        if key in self._filenames:
            fn = self._filenames[key]
        else:
            model_filename = value.meta.filename
            if model_filename is None:
                model_filename = "model.fits"
            subdir = os.path.join(self._dir, f"{key}")
            os.makedirs(subdir)
            fn = os.path.join(subdir, model_filename)
            self._filenames[key] = fn

        # save the model to the temporary location
        value.save(fn)

    def __del__(self):
        if hasattr(self, '_tempdir'):
            self._tempdir.cleanup()

    def __delitem__(self, key):
        del self._filenames[key]

    def __iter__(self):
        return iter(self._filenames)

    def __len__(self):
        return len(self._filenames)


class ModelLibrary(Sequence):
    def __init__(self, init, asn_exptypes=None, asn_n_members=None, on_disk=False, memmap=False, temp_directory=None):
        self._asn_exptypes = asn_exptypes
        self._asn_n_members = asn_n_members
        self._on_disk = on_disk

        self._open = False
        self._ledger = {}

        # FIXME is there a cleaner way to pass these along to datamodels.open?
        self._memmap = memmap

        if self._on_disk:
            self._model_store = _OnDiskModelStore(memmap, temp_directory)
        else:
            self._model_store = {}

        # TODO path support
        # TODO model list support
        if isinstance(init, (str, Path)):
            self._asn_path = os.path.abspath(os.path.expanduser(os.path.expandvars(init)))
            self._asn_dir = os.path.dirname(self._asn_path)
            # load association
            # TODO why did ModelContainer make this local?
            from ..associations import AssociationNotValidError, load_asn
            try:
                with open(self._asn_path) as asn_file:
                    asn_data = load_asn(asn_file)
            except AssociationNotValidError as e:
                raise IOError("Cannot read ASN file.") from e

            if self._asn_exptypes is not None:
                asn_data['products'][0]['members'] = [
                    m for m in asn_data['products'][0]['members']
                    if m['exptype'] in self._asn_exptypes
                ]

            if self._asn_n_members is not None:
                asn_data['products'][0]['members'] = asn_data['products'][0]['members'][:self._asn_n_members]

            # make members easier to access
            self._asn = asn_data
            self._members = self._asn['products'][0]['members']

            # check that all members have a group_id
            # TODO base this off of the model
            for member in self._members:
                if 'group_id' not in member:
                    filename = os.path.join(self._asn_dir, member['expname'])
                    member['group_id'] = _file_to_group_id(filename)

        elif isinstance(init, self.__class__):
            # TODO clone/copy?
            raise NotImplementedError()

        # make sure first model is loaded in memory (as expected by stpipe)
        if self._asn_n_members == 1:
            # FIXME stpipe also reaches into _models (instead of _model_store)
            self._models = [self._load_member(0)]

    def __del__(self):
        # FIXME when stpipe no longer uses '_models'
        if hasattr(self, '_models'):
            self._models[0].close()

    @property
    def asn(self):
        # return a "read only" association
        def _to_read_only(obj):
            if isinstance(obj, dict):
                return MappingProxyType(obj)
            if isinstance(obj, list):
                return tuple(obj)
            return obj
        return asdf.treeutil.walk_and_modify(self._asn, _to_read_only)

    # TODO we may want to not expose this as it could go out-of-sync
    # pretty easily with the actual models.
    # @property
    # def members(self):
    #     return self.asn['products'][0]['members']

    @property
    def group_names(self):
        names = set()
        for member in self._members:
            names.add(member['group_id'])
        return names

    @property
    def group_indices(self):
        group_dict = {}
        for (i, member) in enumerate(self._members):
            group_id = member['group_id']
            if group_id not in group_dict:
                group_dict[group_id] = []
            group_dict[group_id].append(i)
        return group_dict

    def __len__(self):
        return len(self._members)

    def __getitem__(self, index):
        if not self._open:
            raise ClosedLibraryError("ModelLibrary is not open")

        # if model was already borrowed, raise
        if index in self._ledger:
            raise BorrowError("Attempt to double-borrow model")

        if index in self._model_store:
            model = self._model_store[index]
        else:
            model = self._load_member(index)
            if not self._on_disk:
                # it's ok to keep this in memory since _on_disk is False
                self._model_store[index] = model

        # track the model is "in use"
        self._ledger[index] = model
        return model

    def __setitem__(self, index, model):
        if index not in self._ledger:
            raise BorrowError("Attempt to return non-borrowed model")

        # un-track this model
        del self._ledger[index]

        # and store it
        self._model_store[index] = model

        # TODO should we allow this to change group_id for the member?

    def discard(self, index, model):
        # TODO it might be worth allowing `discard(model)` by adding
        # an index of {id(model): index} to the ledger to look up the index
        if index not in self._ledger:
            raise BorrowError("Attempt to discard non-borrowed model")

        # un-track this model
        del self._ledger[index]
        # but do not store it

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def _load_member(self, index):
        member = self._members[index]
        filename = os.path.join(self._asn_dir, member['expname'])

        model = datamodels_open(filename, memmap=self._memmap)

        # patch model metadata with asn member info
        # TODO asn.table_name asn.pool_name here?
        for attr in ('group_id', 'tweakreg_catalog', 'exptype'):
            if attr in member:
                setattr(model.meta, attr, member[attr])
        # this returns an OPEN model, it's up to calling code to close this
        return model

    # TODO save, required by stpipe

    # TODO crds_observatory, get_crds_parameters, when stpipe uses these...

    def _to_container(self):
        # create a temporary directory
        tmpdir = tempfile.TemporaryDirectory(dir='')

        # write out all models (with filenames from member list)
        fns = []
        with self:
            for (i, model) in enumerate(self):
                fn = os.path.join(tmpdir.name, model.meta.filename)
                model.save(fn)
                fns.append(fn)
                self[i] = model

        # use the new filenames for the container
        # copy over "in-memory" options
        # init with no "models"
        container = ModelContainer(fns, save_open=not self._on_disk, return_open=not self._on_disk)
        # give the model container a reference to the temporary directory so it's not deleted
        container._tmpdir = tmpdir
        # FIXME container with filenames already skip finalize_result
        return container

    def finalize_result(self, step, reference_files_used):
        with self:
            for (i, model) in enumerate(self):
                step.finalize_result(model, reference_files_used)
                self[i] = model

    def __enter__(self):
        self._open = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._open = False
        # if exc_value:
        #     # if there is already an exception, don't worry about checking the ledger
        #     # instead allowing the calling code to raise the original error to provide
        #     # a more useful feedback without any chained ledger exception about
        #     # un-returned models
        #     return
        if self._ledger:
            raise BorrowError(f"ModelLibrary has {len(self._ledger)} un-returned models") from exc_value

    def index(self, attribute, copy=False):
        """
        Access a single attribute from all models
        """
        # TODO we could here implement efficient accessors for
        # certain attributes (like `meta.wcs` or `meta.wcs_info.s_region`)
        if copy:
            copy_func = lambda value: value.copy()
        else:
            copy_func = lambda value: value
        with self:
            for (i, model) in range(len(self)):
                attr = model[attribute]
                self.discard(i, model)
                yield copy_func(attr)


def _attrs_to_group_id(
        program_number,
        observation_number,
        visit_number,
        visit_group,
        sequence_id,
        activity_id,
        exposure_number,
    ):
    return (
        f"jw{program_number}{observation_number}{visit_number}"
        f"_{visit_group}{sequence_id}{activity_id}"
        f"_{exposure_number}"
    )


def _file_to_group_id(filename):
    """
    Compute a "group_id" without loading the file
    as a DataModel
    """
    # use astropy.io.fits directly to read header keywords
    # avoiding the DataModel overhead
    # TODO look up attribute to keyword in core schema
    with fits.open(filename) as ff:
        if "ASDF" in ff:
            asdf_yaml = asdf.util.load_yaml(io.BytesIO(ff['ASDF'].data.tobytes()))
            if group_id := asdf_yaml.get('meta', {}).get('group_id'):
                return group_id
        header = ff["PRIMARY"].header
        program_number = header["PROGRAM"]
        observation_number = header["OBSERVTN"]
        visit_number = header["VISIT"]
        visit_group = header["VISITGRP"]
        sequence_id = header["SEQ_ID"]
        activity_id = header["ACT_ID"]
        exposure_number = header["EXPOSURE"]

    return _attrs_to_group_id(
        program_number,
        observation_number,
        visit_number,
        visit_group,
        sequence_id,
        activity_id,
        exposure_number,
    )


def _model_to_group_id(model):
    """
    Compute a "group_id" from a model
    """
    return _attrs_to_group_id(
        model.meta.observation.program_number,
        model.meta.observation.observation_number,
        model.meta.observation.visit_number,
        model.meta.observation.visit_group,
        model.meta.observation.sequence_id,
        model.meta.observation.activity_id,
        model.meta.observation.exposure_number,
    )
