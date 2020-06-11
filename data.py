import os
import torch
import torchvision.datasets as datasets
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler
from torch.utils.data import Subset
from torch._utils import _accumulate
from utils.regime import Regime
from utils.dataset import IndexedFileDataset
from preprocess import get_transform
from itertools import chain
from copy import deepcopy
import warnings
import numpy as np
from PIL import Image

warnings.filterwarnings("ignore", "(Possibly )?corrupt EXIF data", UserWarning)


def get_dataset(name, split='train', transform=None,
                target_transform=None, download=True, datasets_path='~/Datasets'):
    train = (split == 'train')
    root = os.path.join(os.path.expanduser(datasets_path), name)
    if name == 'cifar10':
        return datasets.CIFAR10(root=root,
                                train=train,
                                transform=transform,
                                target_transform=target_transform,
                                download=download)
    elif name == 'cifar100':
        return datasets.CIFAR100(root=root,
                                 train=train,
                                 transform=transform,
                                 target_transform=target_transform,
                                 download=download)
    elif name == 'mnist':
        return datasets.MNIST(root=root,
                              train=train,
                              transform=transform,
                              target_transform=target_transform,
                              download=download)
    elif name == 'stl10':
        return datasets.STL10(root=root,
                              split=split,
                              transform=transform,
                              target_transform=target_transform,
                              download=download)
    elif name == 'imagenet':
        if train:
            root = os.path.join(root, 'train')
        else:
            root = os.path.join(root, 'val')
        return datasets.ImageFolder(root=root,
                                    transform=transform,
                                    target_transform=target_transform)
    elif name == 'imagenet_calib':
        if train:
            root = os.path.join(root.replace('imagenet_calib','imagenet'), 'calib')
        else:
            root = os.path.join(root, 'val')
        return datasets.ImageFolder(root=root,
                                    transform=transform,
                                    target_transform=target_transform)       
    elif name == 'imagenet_calib_10K':
        if train:
            root = os.path.join(root.replace('imagenet_calib_10K','imagenet'), 'calib_10K')
        else:
            root = os.path.join(root, 'val')
        return datasets.ImageFolder(root=root,
                                    transform=transform,
                                    target_transform=target_transform)                                                                   
    elif name == 'imagenet_tar':
        if train:
            root = os.path.join(root, 'imagenet_train.tar')
        else:
            root = os.path.join(root, 'imagenet_validation.tar')
        return IndexedFileDataset(root, extract_target_fn=(
            lambda fname: fname.split('/')[0]),
            transform=transform,
            target_transform=target_transform)


_DATA_ARGS = {'name', 'split', 'transform',
              'target_transform', 'download', 'datasets_path'}
_DATALOADER_ARGS = {'batch_size', 'shuffle', 'sampler', 'batch_sampler',
                    'num_workers', 'collate_fn', 'pin_memory', 'drop_last',
                    'timeout', 'worker_init_fn'}
_TRANSFORM_ARGS = {'transform_name', 'input_size', 'scale_size', 'normalize', 'augment',
                   'cutout', 'duplicates', 'num_crops', 'autoaugment'}
_OTHER_ARGS = {'distributed'}


#class ImageNetCalib(datasets.ImageFolder):
#    """Small calibration dataset taken from training."""
#
#    def __init__(self, root,transform=None, target_transform=None):
#        """
#        Args:
#            csv_file (string): Path to the csv file with annotations.
#            root_dir (string): Directory with all the images.
#            transform (callable, optional): Optional transform to be applied
#                on a sample.
#        """
#        self.samples,self.target = torch.load(root)
#        self.samples  = Image.fromarray(np.uint8(self.samples.permute(0,2,3,1).contiguous().numpy()))
#        self.root = root
#        self.transform = transform
#        self.target_transform = target_transform
#
#
#    def __len__(self):
#        return len(self.target)
#
#    def __getitem__(self, idx):
#        samples = self.samples[idx]
#        target = self.target[idx]
#        if self.transform is not None:
#            #import pdb; pdb.set_trace()
#            print(samples.shape)
#            samples = self.transform(samples)
#        if self.target_transform is not None:
#            target = self.target_transform(target)
#        return samples,target #,idx


class DataRegime(object):
    def __init__(self, regime, defaults={}):
        self.regime = Regime(regime, deepcopy(defaults))
        self.epoch = 0
        self.steps = None
        self.get_loader(True)

    def get_setting(self):
        setting = self.regime.setting
        loader_setting = {k: v for k,
                          v in setting.items() if k in _DATALOADER_ARGS}
        data_setting = {k: v for k, v in setting.items() if k in _DATA_ARGS}
        transform_setting = {
            k: v for k, v in setting.items() if k in _TRANSFORM_ARGS}
        other_setting = {k: v for k, v in setting.items() if k in _OTHER_ARGS}
        transform_setting.setdefault('transform_name', data_setting['name'])
        return {'data': data_setting, 'loader': loader_setting,
                'transform': transform_setting, 'other': other_setting}

    def get(self, key, default=None):
        return self.regime.setting.get(key, default)

    def get_loader(self, force_update=False, override_settings=None, subset_indices=None):
        if force_update or self.regime.update(self.epoch, self.steps):
            setting = self.get_setting()
            if override_settings is not None:
                setting.update(override_settings)
            self._transform = get_transform(**setting['transform'])
            setting['data'].setdefault('transform', self._transform)
            self._data = get_dataset(**setting['data'])
            if subset_indices is not None:
                self._data = Subset(self._data, subset_indices)
            if setting['other'].get('distributed', False):
                setting['loader']['sampler'] = DistributedSampler(self._data)
                setting['loader']['shuffle'] = None
                # pin-memory currently broken for distributed
                setting['loader']['pin_memory'] = False
            self._sampler = setting['loader'].get('sampler', None)
            self._loader = torch.utils.data.DataLoader(
                self._data, **setting['loader'])
        return self._loader

    def set_epoch(self, epoch):
        self.epoch = epoch
        if self._sampler is not None and hasattr(self._sampler, 'set_epoch'):
            self._sampler.set_epoch(epoch)

    def __len__(self):
        return len(self._data)


class SampledDataLoader(object):
    def __init__(self, dl_list):
        self.dl_list = dl_list
        self.epoch = 0

    def generate_order(self):

        order = [[idx]*len(dl) for idx, dl in enumerate(self.dl_list)]
        order = list(chain(*order))
        g = torch.Generator()
        g.manual_seed(self.epoch)
        return torch.tensor(order)[torch.randperm(len(order), generator=g)].tolist()

    def __len__(self):
        return sum([len(dl) for dl in self.dl_list])

    def __iter__(self):
        order = self.generate_order()

        iterators = [iter(dl) for dl in self.dl_list]
        for idx in order:
            yield next(iterators[idx])
        return


class SampledDataRegime(DataRegime):
    def __init__(self, data_regime_list,  probs, split_data=True):
        self.probs = probs
        self.data_regime_list = data_regime_list
        self.split_data = split_data

    def get_setting(self):
        return [data_regime.get_setting() for data_regime in self.data_regime_list]

    def get(self, key, default=None):
        return [data_regime.get(key, default) for data_regime in self.data_regime_list]

    def get_loader(self, force_update=False):
        settings = self.get_setting()
        if self.split_data:
            dset_sizes = [len(get_dataset(**s['data'])) for s in settings]
            assert len(set(dset_sizes)) == 1, \
                "all datasets should be same size"
            dset_size = dset_sizes[0]
            lengths = [int(prob * dset_size) for prob in self.probs]
            lengths[-1] = dset_size - sum(lengths[:-1])
            indices = torch.randperm(dset_size).tolist()
            indices_split = [indices[offset - length:offset]
                             for offset, length in zip(_accumulate(lengths), lengths)]
            loaders = [data_regime.get_loader(force_update=True, subset_indices=indices_split[i])
                       for i, data_regime in enumerate(self.data_regime_list)]
        else:
            loaders = [data_regime.get_loader(
                force_update=force_update) for data_regime in self.data_regime_list]
        self._loader = SampledDataLoader(loaders)
        self._loader.epoch = self.epoch

        return self._loader

    def set_epoch(self, epoch):
        self.epoch = epoch
        if hasattr(self, '_loader'):
            self._loader.epoch = epoch
        for data_regime in self.data_regime_list:
            if data_regime._sampler is not None and hasattr(data_regime._sampler, 'set_epoch'):
                data_regime._sampler.set_epoch(epoch)

    def __len__(self):
        return sum([len(data_regime._data)
                    for data_regime in self.data_regime_list])


if __name__ == '__main__':
    reg1 = DataRegime(None, {'name': 'imagenet', 'batch_size': 16})
    reg2 = DataRegime(None, {'name': 'imagenet', 'batch_size': 32})
    reg1.set_epoch(0)
    reg2.set_epoch(0)
    mreg = SampledDataRegime([reg1, reg2])

    for x, _ in mreg.get_loader():
        print(x.shape)
