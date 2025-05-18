import os, time, csv
import numpy as np
import torch
from sklearn.metrics import confusion_matrix
from scipy import ndimage
from scipy.ndimage import label
from functools import partial
from surface_distance import compute_surface_distances,compute_surface_dice_at_tolerance, compute_average_surface_distance, compute_robust_hausdorff, compute_surface_overlap_at_tolerance
import monai
from monai.inferers import sliding_window_inference
from monai.data import load_decathlon_datalist
from monai.transforms import AsDiscrete,AsDiscreted,Compose,Invertd,SaveImaged
from monai import transforms, data
from networks.swin3d_unetrv2 import SwinUNETR as SwinUNETR_v2
import nibabel as nib
import math
import warnings
warnings.filterwarnings("ignore")

import argparse
parser = argparse.ArgumentParser(description='liver tumor validation')

# file dir
parser.add_argument('--val_dir', default=None, type=str)
parser.add_argument('--json_dir', default=None, type=str)
parser.add_argument('--save_dir', default='out', type=str)
parser.add_argument('--checkpoint', action='store_true')

parser.add_argument('--log_dir', default=None, type=str)
parser.add_argument('--feature_size', default=16, type=int)
parser.add_argument('--val_overlap', default=0.5, type=float)
parser.add_argument('--num_classes', default=3, type=int)

parser.add_argument('--model', default='unet', type=str)
parser.add_argument('--swin_type', default='base', type=str)


def denoise_pred(pred: np.ndarray):
    """
    # 0: background, 1: liver, 2: tumor.
    pred.shape: (3, H, W, D)
    """
    denoise_pred = np.zeros_like(pred)

    # live_channel = pred[1, ...]
    # labels, nb = label(live_channel)
    # max_sum = -1
    # choice_idx = -1
    # for idx in range(1, nb+1):
    #     component = (labels == idx)
    #     if np.sum(component) > max_sum:
    #         choice_idx = idx
    #         max_sum = np.sum(component)
    # component = (labels == choice_idx)
    # denoise_pred[1, ...] = component
    denoise_pred[1, ...] = pred[1, ...]

    # 膨胀然后覆盖掉liver以外的tumor
    # liver_dilation = ndimage.binary_dilation(denoise_pred[1, ...], iterations=30).astype(bool)
    # denoise_pred[2,...] = pred[2,...].astype(bool) * liver_dilation
    # denoise_pred[2, ...] = pred[2,...]
    # denoise_pred[2, ...] = organ_region_filter_out(pred[1, ...], pred[2,...])
    denoise_pred[2, ...] = pred[1, ...] * pred[2,...]

    denoise_pred[0,...] = 1 - np.logical_or(denoise_pred[1,...], denoise_pred[2,...])

    return denoise_pred

def cal_dice(pred, true):
    intersection = np.sum(pred[true==1]) * 2.0
    dice = intersection / (np.sum(pred) + np.sum(true))
    return dice

def cal_dice_nsd(pred, truth, spacing_mm=(1,1,1), tolerance=2, percent = 95):
    dice = cal_dice(pred, truth)
    # cal nsd
    surface_distances = compute_surface_distances(truth.astype(bool), pred.astype(bool), spacing_mm=spacing_mm)
    nsd = compute_surface_dice_at_tolerance(surface_distances, tolerance)
    rhd = compute_robust_hausdorff(surface_distances, percent)
    sd = max(compute_average_surface_distance(surface_distances))
    return (dice, nsd, sd, rhd)


def _get_model(args):
    inf_size = [96, 96, 96]
    print(args.model)
    if args.model == 'swin_unetrv2':
        if args.swin_type == 'tiny':
            feature_size=12
        elif args.swin_type == 'small':
            feature_size=24
        elif args.swin_type == 'base':
            feature_size=48

        model = SwinUNETR_v2(in_channels=1,
                          out_channels=3,
                          img_size=(96, 96, 96),
                          feature_size=feature_size,
                          patch_size=2,
                          depths=[2, 2, 2, 2],
                          num_heads=[3, 6, 12, 24],
                          window_size=[7, 7, 7])
        
    elif args.model == 'unet':
        from monai.networks.nets import UNet 
        model = UNet(
                    spatial_dims=3,
                    in_channels=1,
                    out_channels=3,
                    channels=(16, 32, 64, 128, 256),
                    strides=(2, 2, 2, 2),
                    num_res_units=2,
                )
    
    else:
        raise ValueError('Unsupported model ' + str(args.model))


    if args.checkpoint:
        checkpoint = torch.load(os.path.join(args.log_dir, 'model.pt'), map_location='cpu')

        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in checkpoint['state_dict'].items():
            new_state_dict[k.replace('backbone.','')] = v
        # load params
        model.load_state_dict(new_state_dict, strict=False)
        print('Use logdir weights')
    else:
        model_dict = torch.load(os.path.join(args.log_dir, 'model.pt'))
        model.load_state_dict(model_dict['state_dict'])
        print('Use logdir weights')

    model = model.cuda()
    model_inferer = partial(sliding_window_inference, roi_size=inf_size, sw_batch_size=1, predictor=model,  overlap=args.val_overlap, mode='gaussian')
    pytorch_total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('Total parameters count', pytorch_total_params)

    return model, model_inferer

class LoadImage_val(transforms.LoadImaged):
    def __init__(self, keys, *args,**kwargs, ):
        super().__init__(keys)

    def __call__(self, data):
        d = dict(data)
        data_name = d['name']

        d = super().__call__(d)
        if '05_KiTS' in data_name:
            d['label'][d['label']==3] = 1

        return d

def _get_loader(args):
    val_data_dir = args.val_dir
    datalist_json = args.json_dir 
    val_org_transform = transforms.Compose(
            [
                transforms.LoadImaged(keys=["image", "label"]),
                transforms.AddChanneld(keys=["image", "label"]),
                transforms.Orientationd(keys=["image"], axcodes="RAS"),
                transforms.Spacingd(keys=["image"], pixdim=(1.0, 1.0, 1.0), mode=("bilinear")),
                transforms.ScaleIntensityRanged(keys=["image"], a_min=-175, a_max=250, b_min=0.0, b_max=1.0, clip=True),
                transforms.SpatialPadd(keys=["image"], mode="minimum", spatial_size=[96, 96, 96]),
                transforms.ToTensord(keys=["image", "label"]),
            ]
        )
    val_files = load_decathlon_datalist(datalist_json, True, "validation", base_dir=val_data_dir)
    new_val_files = []
    for item in val_files:
        new_item = {}
        new_item['name'] = item['image']
        new_item['image'] = item['image']
        new_item['label'] = item['label'].replace(val_data_dir+'datafolds', './datafolds')
        new_val_files.append(new_item)
        print(new_item['image'], new_item['label'])

    
    val_org_ds = data.Dataset(new_val_files, transform=val_org_transform)
    val_org_loader = data.DataLoader(val_org_ds, batch_size=1, shuffle=False, num_workers=4, sampler=None, pin_memory=True)

    post_transforms = Compose([
        Invertd(
            keys="pred",
            transform=val_org_transform,
            orig_keys="image",
            meta_keys="pred_meta_dict",
            orig_meta_keys="image_meta_dict",
            meta_key_postfix="meta_dict",
            nearest_interp=False,
            to_tensor=True,
        ),
        # AsDiscreted(keys="pred", argmax=True, to_onehot=3),
        AsDiscreted(keys="pred", argmax=True, to_onehot=3),
        AsDiscreted(keys="label", to_onehot=3),
        # SaveImaged(keys="pred", meta_keys="pred_meta_dict", output_dir=output_dir, output_postfix="seg", resample=False,output_dtype=np.uint8,separate_folder=False),
    ])
    
    return val_org_loader, post_transforms

def main():
    args = parser.parse_args()
    model_name = args.log_dir.split('/')[-1]
    args.model_name = model_name
    print("MAIN Argument values:")
    for k, v in vars(args).items():
        print(k, '=>', v)
    print('-----------------')

    torch.cuda.set_device(0) #use this default device (same as args.device if not distributed)
    torch.backends.cudnn.benchmark = True

    ## loader and post_transform
    val_loader, post_transforms = _get_loader(args)

    ## NETWORK
    model, model_inferer = _get_model(args)

    liver_dice = []
    liver_nsd  = []
    liver_sd = []
    liver_rhd = []
    tumor_dice = []
    tumor_nsd  = []
    tumor_sd = []
    tumor_rhd = []
    header = ['name', 'organ_dice', 'organ_nsd', 'organ_sd', 'organ_rhd', 'tumor_dice', 'tumor_nsd', 'tumor_sd', 'tumor_rhd']
    rows = []

    model.eval()
    start_time = time.time()
    with torch.no_grad():
        for idx, val_data in enumerate(val_loader):
            val_inputs = val_data["image"].cuda()
            name = val_data['label_meta_dict']['filename_or_obj'][0].split('/')[-1].split('.')[0]
            original_affine = val_data["label_meta_dict"]["affine"][0].numpy()
            pixdim = val_data['label_meta_dict']['pixdim'].cpu().numpy()
            spacing_mm = tuple(pixdim[0][1:4])
            val_data['label'][val_data['label']==3] = 1
            val_data["pred"] = model_inferer(val_inputs)
            val_data = [post_transforms(i) for i in data.decollate_batch(val_data)]
            # val_outputs, val_labels = from_engine(["pred", "label"])(val_data)
            val_outputs, val_labels = val_data[0]['pred'], val_data[0]['label'] 
            
            # val_outpus.shape == val_labels.shape  (3, H, W, Z)
            val_outputs, val_labels = val_outputs.detach().cpu().numpy(), val_labels.detach().cpu().numpy()

            # denoise the ouputs 
            # val_outputs = denoise_pred(val_outputs)
            
            # print(val_outputs.shape, val_labels.shape)

            current_liver_dice, current_liver_nsd, current_liver_sd, current_liver_rhd = cal_dice_nsd(val_outputs[1,...], val_labels[1,...], spacing_mm=spacing_mm)
            current_tumor_dice, current_tumor_nsd, current_tumor_sd, current_tumor_rhd = cal_dice_nsd(val_outputs[2,...], val_labels[2,...], spacing_mm=spacing_mm)
            
            # print(current_liver_dice, current_liver_nsd, current_liver_sd, current_liver_rhd)
            # print(current_tumor_dice, current_tumor_nsd, current_tumor_sd, current_tumor_rhd)
            if math.isinf(current_tumor_sd):
                current_tumor_sd = 100
                print('inf')
            
            if math.isinf(current_tumor_rhd):
                current_tumor_rhd = 200
                print('inf')
            
            
            liver_dice.append(current_liver_dice)
            liver_nsd.append(current_liver_nsd)
            liver_sd.append(current_liver_sd)
            liver_rhd.append(current_liver_rhd)
            tumor_dice.append(current_tumor_dice)
            tumor_nsd.append(current_tumor_nsd)
            tumor_sd.append(current_tumor_sd)
            tumor_rhd.append(current_tumor_rhd)
            row = [name, current_liver_dice, current_liver_nsd, current_liver_sd, current_liver_rhd, current_tumor_dice, current_tumor_nsd, current_tumor_sd, current_tumor_rhd]
            rows.append(row)

            print(name, val_outputs[0].shape, \
                'dice: [{:.3f}  {:.3f}]; nsd: [{:.3f}  {:.3f}]'.format(current_liver_dice, current_tumor_dice, current_liver_nsd, current_tumor_nsd), \
                'sd: [{:.3f}  {:.3f}]; rhd: [{:.3f}  {:.3f}]'.format(current_liver_sd, current_tumor_sd, current_liver_rhd, current_tumor_rhd), \
                'time {:.2f}s'.format(time.time() - start_time))

            # save the prediction
            output_dir = os.path.join(args.save_dir, args.model_name, str(args.val_overlap), 'pred')
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            val_outputs = np.argmax(val_outputs, axis=0)

            nib.save(
                nib.Nifti1Image(val_outputs.astype(np.uint8), original_affine), os.path.join(output_dir, f'{name}.nii.gz')
            )


        print("organ dice:", np.mean(liver_dice))
        print("organ nsd:", np.mean(liver_nsd))
        print("organ sd:", np.mean(liver_sd))
        print("organ rhd:", np.mean(liver_rhd))
        print("tumor dice:", np.mean(tumor_dice))
        print("tumor nsd",np.mean(tumor_nsd))
        print("tumor sd:", np.mean(tumor_sd))
        print("tumor rhd:", np.mean(tumor_rhd))
        
        results = [
                ["organ dice", np.mean(liver_dice)],
                ["organ nsd", np.mean(liver_nsd)],
                ["organ sd", np.mean(liver_sd)],
                ["organ rhd", np.mean(liver_rhd)],
                ["tumor dice", np.mean(tumor_dice)],
                ["tumor nsd", np.mean(tumor_nsd)],
                ["tumor sd", np.mean(tumor_sd)],
                ["tumor rhd", np.mean(tumor_rhd)]
            ]

        # save metrics to cvs file
        csv_save = os.path.join(args.save_dir, args.model_name, str(args.val_overlap))
        if not os.path.exists(csv_save):
            os.makedirs(csv_save)
        csv_name = os.path.join(csv_save, 'metrics.csv')
        with open(csv_name, 'w', encoding='UTF8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
            writer.writerows(results)

# save path: save_dir/log_dir_name/str(args.val_overlap)/pred/
if __name__ == "__main__":
    main()

