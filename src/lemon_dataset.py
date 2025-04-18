import torch
from torch.utils.data import Dataset, DataLoader
import numpy as np
import os
import xarray as xr
import pandas as pd
from scipy.signal import butter, sosfiltfilt


class LEMONDataset(Dataset):
    def __init__(
            self,
            data_dir,
            channels=['O1', 'O2', 'F1', 'F2', 'C1', 'C2', 'P1', 'P2'],
            downstream_task='age',
            bandpass_filter=0.5,
            segment_size: int = 512,
            mode='train',
    ):
        super(LEMONDataset, self).__init__()
        self.mode = mode
        self.channels = channels
        
        # eeg
        data_path = os.path.join(data_dir, 'EC_all_channels_processed_downsampled.nc5')
        self.eeg = xr.open_dataarray(data_path, engine='h5netcdf')
        self.subjects = self.eeg.subject.values

        # demographics
        _demographics_path = os.path.join(data_dir, 'Demographics.csv')
        demog = pd.read_csv(_demographics_path, index_col="ID")

        beh_score = None
        if downstream_task.lower() == 'upps':
            # NOTE behavioral score    
            beh_score = pd.read_csv('data/LEMON/UPPS.csv', index_col="ID")
            beh_score_name = 'UPPS_sens_seek'
            beh_score[beh_score_name] = (
                beh_score[beh_score_name].apply(lambda x: 1 if x > 30 else 0))
            # merge behavioral scores into eeg
            demog = demog.merge(beh_score, left_index=True, right_index=True)
            beh_score = demog.loc[self.subjects, beh_score_name]
            self.eeg = self.eeg.assign_coords(beh_score=("subject", beh_score))
        elif downstream_task.lower() == 'age':
            # age (young/old)
            demog['is_old'] = demog['Age'].apply(
                lambda x:1 if int(x.split('-')[0]) >= 50 else 0)
            beh_score = demog.loc[self.eeg["subject"].values, "is_old"]
            self.eeg = self.eeg.assign_coords(beh_score=("subject", beh_score))
        elif downstream_task.lower() == 'gender':
            beh_score = demog.loc[self.eeg["subject"].values, "Gender_ 1=female_2=male"]
            beh_score = beh_score.apply(lambda x: 1 if x == 2 else 0)
            self.eeg = self.eeg.assign_coords(beh_score=("subject", beh_score))

        # downsample FIXME
        n_y0 = (beh_score == 0).sum()
        n_y1 = (beh_score == 1).sum()
        n_min = min(n_y0, n_y1)  # mini sample per label
        self.n_subjects = n_min * 2
        y0_sub_ids = beh_score[beh_score == 0].index[:n_min]
        y1_sub_ids = beh_score[beh_score == 1].index[:n_min]
        sub_ids = y0_sub_ids.append(y1_sub_ids)
        self.eeg = self.eeg.sel(subject=sub_ids)

        # select channels
        x = self.eeg.sel(channel=channels).to_numpy()
        print("XXXX.shape: ", x.shape)
        if bandpass_filter is not None:
            sos = butter(4, bandpass_filter, btype='high', fs=128, output='sos')  # TODO: fs
            x = sosfiltfilt(sos, x, axis=-1)

        # segment signals
        x = torch.tensor(x.copy()).unfold(2, segment_size, segment_size).permute(0, 2, 3, 1).flatten(0, 1)  # TODO: copy was added to x because of an error, look into this

        # x.shape: bz, seq_len, ch_num
        # TODO expected: bz, ch_num, seq_len, patch_size
        patch_size = 200

        self.subject_ids = torch.tensor(np.arange(0, self.n_subjects).repeat(x.shape[0] // self.n_subjects)[:, np.newaxis])
        self.subject_ids = self.subject_ids.squeeze()

        x = x.permute(0, 2, 1)
        x = x.unfold(2, patch_size, patch_size)
        self.x = x.float()

        self.y = self.eeg.beh_score.values
        self.y = self.y.repeat(self.x.shape[0] / self.n_subjects)

        # TODO test/train split
        indices = torch.randperm(self.y.shape[0])
        self.x=self.x[indices]
        self.y=self.y[indices]

        if self.mode == 'train':
            self.x = self.x[:int(len(self.x) * 0.8)]
            self.y = self.y[:int(len(self.y) * 0.8)]
            self.subject_ids = self.subject_ids[:int(len(self.subject_ids) * 0.8)]
        elif self.mode == 'val':
            self.x = self.x[int(len(self.x) * 0.8):int(len(self.x) * 0.9)]
            self.y = self.y[int(len(self.y) * 0.8):int(len(self.y) * 0.9)]
            self.subject_ids = self.subject_ids[int(len(self.subject_ids) * 0.8):int(len(self.subject_ids) * 0.9)]
        elif self.mode == 'test':
            self.x = self.x[int(len(self.x) * 0.9):int(len(self.x) * 1.)]
            self.y = self.y[int(len(self.y) * 0.9):int(len(self.y) * 1.)]
            self.subject_ids = self.subject_ids[int(len(self.subject_ids) * 0.9):int(len(self.subject_ids) * 1.)]


    def __len__(self):
        return len((self.y))

    def __getitem__(self, idx):
        x_idx = self.x[idx]
        y_idx = self.y[idx]
        return x_idx, y_idx

    def collate(self, batch):
        x = np.array([x[0] for x in batch])
        y = np.array([x[1] for x in batch])
        return torch.from_numpy(x).float(), torch.from_numpy(y).float()


class LoadDataset(object):
    def __init__(self, params):
        self.params = params
        self.data_dir = params.data_dir
        self.channels = params.channels
        self.bandpass_filter = params.bandpass_filter
        self.segment_size = params.segment_size

    def get_data_loader(self):
        train_set = LEMONDataset(self.data_dir, mode='train')
        val_set = LEMONDataset(self.data_dir, mode='val')
        test_set = LEMONDataset(self.data_dir, mode='test')
        print("train,val,test: ", len(train_set), len(val_set), len(test_set))
        print("total: ", len(train_set)+len(val_set)+len(test_set))
        data_loader = {
            'train': DataLoader(
                train_set,
                batch_size=self.params.batch_size,
                collate_fn=train_set.collate,
                shuffle=True,
            ),
            'val': DataLoader(
                val_set,
                batch_size=self.params.batch_size,
                collate_fn=val_set.collate,
                shuffle=True,
            ),
            'test': DataLoader(
                test_set,
                batch_size=self.params.batch_size,
                collate_fn=test_set.collate,
                shuffle=True,
            ),
        }
        return data_loader
