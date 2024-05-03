import json

from jwst.associations.asn_from_list import asn_from_list
from jwst.associations.load_as_asn import load_asn
from jwst.datamodels.library import ModelLibrary
import jwst.datamodels as dm

import stdatamodels.jwst.datamodels
from stdatamodels.jwst.datamodels import ImageModel

import pytest


# for the example association, set 2 different observation numbers
# so the association will have 2 groups (since all other group_id
# determining meta is the same, see `example_asn_path`)
_OBSERVATION_NUMBERS = ['1', '1', '2']
_N_MODELS = len(_OBSERVATION_NUMBERS)
_N_GROUPS = len(set(_OBSERVATION_NUMBERS))


@pytest.fixture
def example_asn_path(tmp_path):
    fns = []
    for i in range(_N_MODELS):
        m = ImageModel()
        m.meta.observation.program_number = '0001'
        m.meta.observation.observation_number = _OBSERVATION_NUMBERS[i]
        m.meta.observation.visit_number = '1'
        m.meta.observation.visit_group = '1'
        m.meta.observation.sequence_id = '01'
        m.meta.observation.activity_id = '1'
        m.meta.observation.exposure_number = '1'
        m.meta.instrument.name = 'NIRCAM'
        m.meta.instrument.channel = 'SHORT'
        base_fn = f'{i}.fits'
        m.meta.filename = base_fn
        m.save(str(tmp_path / base_fn))
        fns.append(base_fn)
    asn = asn_from_list(fns, product_name="foo_out")
    base_fn, contents = asn.dump(format="json")
    asn_filename = tmp_path / base_fn
    with open(asn_filename, 'w') as f:
        f.write(contents)
    return asn_filename


def _set_custom_member_attr(example_asn_path, member_index, attr, value):
    with open(example_asn_path, 'r') as f:
        asn_data = load_asn(f)
    asn_data['products'][0]['members'][member_index][attr] = value
    with open(example_asn_path, 'w') as f:
        json.dump(asn_data, f)


def test_load_asn(example_asn_path):
    library = ModelLibrary(example_asn_path)
    assert len(library) == _N_MODELS


@pytest.mark.parametrize("asn_n_members", range(_N_MODELS))
def test_asn_n_members(example_asn_path, asn_n_members):
    library = ModelLibrary(example_asn_path, asn_n_members=asn_n_members)
    assert len(library) == asn_n_members


def test_asn_exptypes(example_asn_path):
    _set_custom_member_attr(example_asn_path, 0, 'exptype', 'background')
    library = ModelLibrary(example_asn_path, asn_exptypes='science')
    assert len(library) == _N_MODELS - 1
    library = ModelLibrary(example_asn_path, asn_exptypes='background')
    assert len(library) == 1


# memmap?
# temp_directory

# strictness, what can be done when 'open'?
# non-returned model
# setitem vs discard

def test_group_names(example_asn_path):
    library = ModelLibrary(example_asn_path)
    assert len(library.group_names) == _N_GROUPS
    group_names = set()
    with library:
        for index, model in enumerate(library):
            group_names.add(model.meta.group_id)
            library.discard(index, model)
    assert group_names == set(library.group_names)


def test_group_indices(example_asn_path):
    library = ModelLibrary(example_asn_path)
    group_indices = library.group_indices
    assert len(group_indices) == _N_GROUPS
    with library:
        for group_name in group_indices:
            indices = group_indices[group_name]
            for index in indices:
                model = library[index]
                assert model.meta.group_id == group_name
                library.discard(index, model)


@pytest.mark.parametrize("attr", ["group_names", "group_indices"])
def test_group_no_load(example_asn_path, attr, monkeypatch):
    # patch datamodels.open to always raise an exception
    # this will serve as a smoke test to see if any of the attribute
    # accesses (or instance creation) attempts to open models
    def no_open(*args, **kwargs):
        raise Exception()

    monkeypatch.setattr(stdatamodels.jwst.datamodels, 'open', no_open)

    library = ModelLibrary(example_asn_path)
    getattr(library, attr)


@pytest.mark.parametrize(
    "asn_group_id, meta_group_id, expected_group_id", [
        ('42', None, '42'),
        (None, '42', '42'),
        ('42', '26', '42'),
    ])
def test_group_id_override(example_asn_path, asn_group_id, meta_group_id, expected_group_id):
    if asn_group_id:
        _set_custom_member_attr(example_asn_path, 0, 'group_id', asn_group_id)
    if meta_group_id:
        model_filename = example_asn_path.parent / '0.fits'
        with dm.open(model_filename) as model:
            model.meta.group_id = meta_group_id
            model.save(model_filename)
    library = ModelLibrary(example_asn_path)
    group_names = library.group_names
    assert len(group_names) == 3
    assert expected_group_id in group_names
    with library:
        model = library[0]
        assert model.meta.group_id == expected_group_id
        library.discard(0, model)


def test_model_iteration(example_asn_path):
    library = ModelLibrary(example_asn_path)
    with library:
        for i, model in enumerate(library):
            assert int(model.meta.filename.split('.')[0]) == i
            library.discard(i, model)


def test_model_indexing(example_asn_path):
    library = ModelLibrary(example_asn_path)
    with library:
        for i in range(_N_MODELS):
            model = library[i]
            assert int(model.meta.filename.split('.')[0]) == i
            library.discard(i, model)

# test on-disk behavior

# test asn API
# test asn data is read-only

# stpipe _models requirement

# container conversion

# exception handling

# index
