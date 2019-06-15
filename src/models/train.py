from pprint import pprint

import imgaug.augmenters as iaa
import mlflow.pytorch
import numpy as np
import torch
from torch.utils.data import DataLoader
from torchvision.transforms import Compose
from tqdm import tqdm

from src import XR_HAND_CROPPED_PATH, MODELS_DIR, MLFLOW_TRACKING_URI, XR_HAND_PATH
from src.data import TrainValTestSplitter, MURASubset
from src.data.transforms import GrayScale, Padding, Resize, HistEqualisation, MinMaxNormalization, ToTensor
from src.features.augmentation import Augmentation
# from src.models import BottleneckAutoencoder, BaselineAutoencoder
from src.models.gans import DCGAN
from src.utils import query_yes_no

# ---------------------------------------  Parameters setups ---------------------------------------
# Ignoring numpy warnings and setting seeds
np.seterr(divide='ignore', invalid='ignore')
torch.manual_seed(42)

model_class = DCGAN
device = "cuda" if torch.cuda.is_available() else "cpu"
# device = 'cpu'
num_workers = 7
log_to_mlflow = query_yes_no('Log this run to mlflow?', 'no')

# Mlflow settings
mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
mlflow.set_experiment(model_class.__name__)

# Mlflow parameters
run_params = {
    'batch_size': 128,
    'image_resolution': (512, 512),
    'num_epochs': 1,
    'batch_normalisation': True,
    'pipeline': {
        'hist_equalisation': False,
        'cropped': True,
    },
    'masked_loss_on_val': True,
    'masked_loss_on_train': True,
}

# Data source
data_path = XR_HAND_CROPPED_PATH if run_params['pipeline']['cropped'] else XR_HAND_PATH

# Augmentation
augmentation_seq = iaa.Sequential([iaa.Fliplr(0.5),  # horizontally flip 50% of all images
                                   iaa.Flipud(0.5),  # vertically flip 50% of all images,
                                   iaa.Sometimes(0.5, iaa.Affine(fit_output=True,  # not crop corners by rotation
                                                                 rotate=(-20, 20),  # rotate by -45 to +45 degrees
                                                                 order=[0, 1])),
                                   # use nearest neighbour or bilinear interpolation (fast)
                                   # iaa.Resize(),
                                   # iaa.PadToFixedSize(512, 512, position='uniform')
                                   ])
run_params['augmentation'] = augmentation_seq.get_all_children()


# ----------------------------- Data, preprocessing and model initialization ------------------------------------
composed_transforms = Compose([GrayScale(),
                               HistEqualisation(active=run_params['pipeline']['hist_equalisation']),
                               Augmentation(augmentation_seq),
                               Resize(run_params['image_resolution'], keep_aspect_ratio=True),
                               Padding(max_shape=run_params['image_resolution']),
                               # max_shape - max size of image after augmentation
                               MinMaxNormalization(),
                               ToTensor()])
# Preprocessing pipeline

# Dataset loaders
print(f'\nDATA SPLIT:')
splitter = TrainValTestSplitter(path_to_data=data_path)
train = MURASubset(filenames=splitter.data_train.path, patients=splitter.data_train.patient,
                   transform=composed_transforms, true_labels=np.zeros(len(splitter.data_train.path)))
validation = MURASubset(filenames=splitter.data_val.path, true_labels=splitter.data_val.label,
                        patients=splitter.data_val.patient, transform=composed_transforms)
test = MURASubset(filenames=splitter.data_test.path, true_labels=splitter.data_test.label,
                  patients=splitter.data_test.patient, transform=composed_transforms)

train_loader = DataLoader(train, batch_size=run_params['batch_size'], shuffle=True, num_workers=num_workers)
val_loader = DataLoader(validation, batch_size=run_params['batch_size'], shuffle=True, num_workers=num_workers)
test_loader = DataLoader(test, batch_size=run_params['batch_size'], shuffle=True, num_workers=num_workers)

# Model initialization
model = model_class(device=device,
                    use_batchnorm=run_params['batch_normalisation'],
                    masked_loss_on_val=run_params['masked_loss_on_val'],
                    masked_loss_on_train=run_params['masked_loss_on_train']).to(device)
# model = torch.load(f'{MODELS_DIR}/{model_class.__name__}.pth')
# model.eval().to(device)
print(f'\nMODEL ARCHITECTURE:')
trainable_params = model.summary(device=device, image_resolution=run_params['image_resolution'])
run_params['trainable_params'] = trainable_params


# -------------------------------- Logging ------------------------------------
# Logging
print('\nRUN PARAMETERS:')
pprint(run_params, width=-1)

if log_to_mlflow:
    for (param, value) in run_params.items():
        mlflow.log_param(param, value)


# -------------------------------- Training and evaluation -----------------------------------
val_metrics = None
for epoch in range(1, run_params['num_epochs'] + 1):

    print('===========Epoch [{}/{}]============'.format(epoch, run_params['num_epochs']))

    for batch_data in tqdm(train_loader, desc='Training', total=len(train_loader)):
        loss = model.train_on_batch(batch_data, device=device, epoch=epoch, num_epochs=run_params['num_epochs'])

    # log
    print(f'Loss on last train batch: {loss.data}')

    # validation
    val_metrics = model.evaluate(val_loader, 'validation', device, log_to_mlflow=log_to_mlflow)

    # forward pass for the random validation image
    index = np.random.randint(0, len(validation), 1)[0]
    # model.forward_and_save_one_image(validation[index]['image'].unsqueeze(0), validation[index]['label'], epoch, device)

print('=========Training ended==========')

# Test performance
model.evaluate(test_loader, 'test', device, log_to_mlflow=log_to_mlflow, val_metrics=val_metrics)

# Saving
mlflow.pytorch.log_model(model, f'{model_class.__name__}')
torch.save(model, f'{MODELS_DIR}/{model_class.__name__}.pth')
