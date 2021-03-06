import sys
import os, csv, random, math, json
import glob

import rasterio
import cv2

from PIL import Image
import numpy as np
import pandas as pd

import skimage.io
from scipy.ndimage import zoom
from skimage.transform import resize

import torch
import torch.utils.data as data
from torch.autograd import Variable

from torchvision.transforms import functional



IMG_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.ppm', '.bmp', '.pgm']
def is_image_file(filename):
    """Checks if a file is an image.

    Args:
        filename (string): path to a file

    Returns:
        bool: True if the filename ends with a known image extension
    """
    filename_lower = filename.lower()
    return any(filename_lower.endswith(ext) for ext in IMG_EXTENSIONS)

def match_band(source, template):
    """
    Adjust the pixel values of a grayscale image such that its histogram
    matches that of a target image
    Arguments:
    -----------
        source: np.ndarray
            Image to transform; the histogram is computed over the flattened
            array
        template: np.ndarray
            Template image; can have different dimensions to source
    Returns:
    -----------
        matched: np.ndarray
            The transformed output image
    """
    oldshape = source.shape
    source = source.ravel()
    template = template.ravel()

    perm = source.argsort(kind='heapsort')

    aux = source[perm]
    flag = np.concatenate(([True], aux[1:] != aux[:-1]))
    s_values = aux[flag]

    iflag = np.cumsum(flag) - 1
    inv_idx = np.empty(source.shape, dtype=np.intp)
    inv_idx[perm] = iflag
    bin_idx = inv_idx

    idx = np.concatenate(np.nonzero(flag) + ([source.size],))
    s_counts = np.diff(idx)

    a = pd.value_counts(template).sort_index()
    t_values = np.asarray(a.index)
    t_counts = np.asarray(a.values)

    s_quantiles = np.cumsum(s_counts).astype(np.float32)
    s_quantiles /= s_quantiles[-1]

    t_quantiles = np.cumsum(t_counts).astype(np.float32)
    t_quantiles /= t_quantiles[-1]

    # interpolate linearly to find the pixel values in the template image
    # that correspond most closely to the quantiles in the source image
    interp_t_values = np.interp(s_quantiles, t_quantiles, t_values)

    return interp_t_values[bin_idx].reshape(oldshape)


def stretch_8bit(band, lower_percent=5, higher_percent=95):
    """stretch_8bit takes a 3 band image (as an array) and returns an 8bit array with clipped values (5-95%) stretched to 0-255

    Parameters
    ----------
    bands : numpy.array
        Numpy array of shape  (*,*,3)
    lower_percent : type
        Lower threshold below which array values will be discarded (the default is 5).
    higher_percent : type
        Upper threshold above which array values will be discarded (the default is 95).

    Returns
    -------
    numpy.array
        Numpy array containing np.uint8 values of shape bands.shape

    """
    a = 0
    b = 255
    real_values = band.flatten()
    real_values = real_values[real_values > 0]
    c = np.percentile(real_values, lower_percent)
    d = np.percentile(real_values, higher_percent)
    t = a + (band - c) * (b - a) / (d - c)
    t[t<a] = a
    t[t>b] = b
    return t.astype(np.uint8)

def find_classes(images_list):

    classes = {}
    class_id = 0
    for image in images_list:
        if image[1] not in classes:
            classes[image[1]] = class_id
            class_id += 1

    return classes.keys(), classes

def make_dataset(dir, images_list, class_to_idx):
    images = []

    for image in images_list:
        images.append((dir + image[0], int(image[1])))

    return images

def pil_loader(path):
    # open path as file to avoid ResourceWarning (https://github.com/python-pillow/Pillow/issues/835)
    with open(path, 'rb') as f:
        img = Image.open(f)
        return img.convert('RGB')


def npy_seq_loader(seq):
    out = []
    for s in seq:
        out.append(np.load(s))
    out = np.asarray(out)

    return out

def get_train_val_metadata(data_dir, val_cities, patch_size, stride):
    cities = os.listdir(data_dir + 'train_labels/')
    cities.sort()
    val_cities = list(map(int, val_cities.split(',')))
    train_cities = list(set(range(len(cities))).difference(val_cities))

    train_metadata = []
    for city_no in train_cities:
        city_label = cv2.imread(data_dir + 'train_labels/' + cities[city_no] + '/cm/cm.png', 0) / 255

        for i in range(0, city_label.shape[0], stride):
            for j in range(0, city_label.shape[1], stride):
                if (i + patch_size) <= city_label.shape[0] and (j + patch_size) <= city_label.shape[1]:
                    train_metadata.append([cities[city_no], i, j])

    val_metadata = []
    for city_no in val_cities:
        city_label = cv2.imread(data_dir + 'train_labels/' + cities[city_no] + '/cm/cm.png', 0) / 255
        for i in range(0, city_label.shape[0], patch_size):
            for j in range(0, city_label.shape[1], patch_size):
                if (i + patch_size) <= city_label.shape[0] and (j + patch_size) <= city_label.shape[1]:
                    val_metadata.append([cities[city_no], i, j])

    return train_metadata, val_metadata

def full_onera_loader(path, bands):
    cities = os.listdir(path + 'train_labels/')

    dataset = {}
    for city in cities:
        if '.txt' not in city:
            base_path1 = glob.glob(path + 'images/' + city + '/imgs_1/*.tif')[0][:-7]
            base_path2 = glob.glob(path + 'images/' + city + '/imgs_2/*.tif')[0][:-7]
            label = cv2.imread(path + 'train_labels/' + city + '/cm/' + 'cm.png', 0) / 255

            bands1_stack = []
            bands2_stack = []
            for band in bands:
                band1_r = rasterio.open(base_path1 + band + '.tif')
                band2_r = rasterio.open(base_path2 + band + '.tif')

                band1_d = band1_r.read()[0]
                band2_d = band2_r.read()[0]

                band2_d = match_band(band2_d, band1_d)

                band1_d = stretch_8bit(band1_d, 2, 98).astype(np.float32) / 255.
                band1_d = cv2.resize(band1_d, (label.shape[1], label.shape[0]))
                bands1_stack.append(band1_d)

                band2_d = stretch_8bit(band2_d, 2, 98).astype(np.float32) / 255.
                band2_d = cv2.resize(band2_d, (label.shape[1], label.shape[0]))
                bands2_stack.append(band2_d)

            two_dates = np.asarray([bands1_stack, bands2_stack])
            two_dates = np.transpose(two_dates, (1,0,2,3))
            dataset[city] = {'images':two_dates , 'labels': label.astype(np.uint8)}

    return dataset

def full_onera_loader_with_pc_priors(path, bands):
    cities = os.listdir(path + 'train_labels/')

    dataset = {}
    for city in cities:
        if '.txt' not in city:
            base_path1 = glob.glob(path + 'images/' + city + '/imgs_1/*.tif')[0][:-7]
            base_path2 = glob.glob(path + 'images/' + city + '/imgs_2/*.tif')[0][:-7]
            label = cv2.imread(path + 'train_labels/' + city + '/cm/' + 'cm.png', 0) / 255
            pc_priors = np.load(path + 'results/patch_classifier_' + city + '_probs.npy').astype(np.float32)

            bands1_stack = []
            bands2_stack = []
            for band in bands:
                band1_r = rasterio.open(base_path1 + band + '.tif')
                band2_r = rasterio.open(base_path2 + band + '.tif')

                band1_d = band1_r.read()[0]
                band2_d = band2_r.read()[0]

                band2_d = match_band(band2_d, band1_d)

                band1_d = stretch_8bit(band1_d, 2, 98).astype(np.float32) / 255.
                band1_d = cv2.resize(band1_d, (label.shape[1], label.shape[0]))
                bands1_stack.append(band1_d)

                band2_d = stretch_8bit(band2_d, 2, 98).astype(np.float32) / 255.
                band2_d = cv2.resize(band2_d, (label.shape[1], label.shape[0]))
                bands2_stack.append(band2_d)

            for prior in pc_priors[0]:
                prior = cv2.resize(prior, (label.shape[1], label.shape[0]))
                bands1_stack.append(prior)

            for prior in pc_priors[1]:
                prior = cv2.resize(prior, (label.shape[1], label.shape[0]))
                bands2_stack.append(prior)

            two_dates = np.asarray([bands1_stack, bands2_stack])
            two_dates = np.transpose(two_dates, (1,0,2,3))
            dataset[city] = {'images':two_dates , 'labels': label.astype(np.uint8)}

    return dataset

def full_onera_multidate_loader(path, bands):
    fin = open(path + 'multidate_metadata.json','r')
    metadata = json.load(fin)
    fin.close()

    cities = os.listdir(path + 'train_labels/')

    dataset = {}
    for city in cities:
        if city in metadata:
            dates_stack = []
            label = cv2.imread(path + 'train_labels/' + city + '/cm/' + 'cm.png', 0) / 255

            first_date = True
            for date_no in range(5):
                bands_stack = []
                base_path = glob.glob(metadata[city][str(date_no)] + '/*.tif')[0][:-7]

                for band_no in range(len(bands)):
                    band_r = rasterio.open(base_path + bands[band_no] + '.tif')
                    band_d = band_r.read()[0]

                    if not first_date:
                        band_d = match_band(band_d, dates_stack[0][band_no])

                    band_d = stretch_8bit(band_d, 2, 98).astype(np.float32) / 255.
                    band_d = cv2.resize(band_d, (label.shape[1], label.shape[0]))
                    bands_stack.append(band_d)

                if not first_date:
                    first_date = False

                dates_stack.append(bands_stack)

            dates_stack = np.asarray(dates_stack).transpose(1,0,2,3)
            dataset[city] = {'images':dates_stack , 'labels': label.astype(np.uint8)}

    return dataset

def full_buildings_loader(path):
    dates = os.listdir(path + 'Images/')
    dates.sort()

    label_r = rasterio.open(path + 'Ground_truth/Changes/Changes_06_11.tif')
    label = label_r.read()[0]

    stacked_dates = []
    for date in dates:
        r = rasterio.open(path + 'Images/' + date)
        d = r.read()
        bands = []
        if d.shape[0] == 4:
            for b in d:
                band = stretch_8bit(b, 0.01, 99)
                band = cv2.resize(band, (label.shape[1], label.shape[0]))
                bands.append(band / 255.)
        if d.shape[0] == 8:
            for b in [1,2,4,7]:
                band = stretch_8bit(d[b], 0.01, 99)
                band = cv2.resize(band, (label.shape[1], label.shape[0]))
                bands.append(band/ 255.)
        stacked_dates.append(bands)

    stacked_dates = np.asarray(stacked_dates).astype(np.float32).transpose(1,0,2,3)

    print (stacked_dates.shape, label.shape)
    return {'images':stacked_dates, 'labels':label.astype(np.uint8)}


def onera_loader(dataset, city, x, y, size, aug):
    out_img = dataset[city]['images'][:, : ,x:x+size, y:y+size]
    if aug:
        out_img = np.rot90(out_img, random.randint(0,3), [2,3])
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=2)
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=2)

    return out_img, dataset[city]['labels'][x:x+size, y:y+size]

def onera_siamese_loader(dataset, city, x, y, size, aug):
    out_img = np.copy(dataset[city]['images'][:, : ,x:x+size, y:y+size])
    out_lbl = np.copy(dataset[city]['labels'][x:x+size, y:y+size])
    if aug:
        rot_deg = random.randint(0,3)
        out_img = np.rot90(out_img, rot_deg, [2,3]).copy()
        out_lbl = np.rot90(out_lbl, rot_deg, [0,1]).copy()
        
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=2).copy()
            out_lbl = np.flip(out_lbl, axis=0).copy()
            
        if random.random() > 0.5:
            out_img = np.flip(out_img, axis=3).copy()
            out_lbl = np.flip(out_lbl, axis=1).copy()
            
    out_img = np.transpose(out_img, (1,0,2,3))
    return out_img[0], out_img[1], out_lbl

def onera_siamese_loader_late_pooling(dataset, city, x, y, size):
    patch = np.transpose(dataset[city]['images'][:, : ,x:x+size, y:y+size], (1,0,2,3))

    p0 = patch[0]
    p1 = patch[1]

    p0_cat1 = np.stack([p0[1],p0[2],p0[3],p0[7]])
    p1_cat1 = np.stack([p1[1],p1[2],p1[3],p1[7]])

    p0_5 = cv2.resize(p0[4], (size//2,size//2))
    p0_6 = cv2.resize(p0[5], (size//2,size//2))
    p0_7 = cv2.resize(p0[6], (size//2,size//2))
    p0_8a = cv2.resize(p0[8], (size//2,size//2))
    p0_11 = cv2.resize(p0[10], (size//2,size//2))
    p0_12 = cv2.resize(p0[11], (size//2,size//2))

    p1_5 = cv2.resize(p1[4], (size//2,size//2))
    p1_6 = cv2.resize(p1[5], (size//2,size//2))
    p1_7 = cv2.resize(p1[6], (size//2,size//2))
    p1_8a = cv2.resize(p1[8], (size//2,size//2))
    p1_11 = cv2.resize(p1[10], (size//2,size//2))
    p1_12 = cv2.resize(p1[11], (size//2,size//2))

    p0_cat2 = np.stack([p0_5, p0_6, p0_6, p0_8a, p0_11, p0_12])
    p1_cat2 = np.stack([p1_5, p1_6, p1_6, p1_8a, p1_11, p1_12])

    cat3_size = int(math.ceil(size/6))
    p0_1 = cv2.resize(p0[0], (cat3_size,cat3_size))
    p0_9 = cv2.resize(p0[9], (cat3_size,cat3_size))
    p0_10 = cv2.resize(p0[10], (cat3_size,cat3_size))

    p1_1 = cv2.resize(p1[0], (cat3_size,cat3_size))
    p1_9 = cv2.resize(p1[9], (cat3_size,cat3_size))
    p1_10 = cv2.resize(p1[10], (cat3_size,cat3_size))

    p0_cat3 = np.stack([p0_1, p0_9, p0_10])
    p1_cat3 = np.stack([p1_1, p1_9, p1_10])

    return p0_cat1, p0_cat2, p0_cat3, p1_cat1, p1_cat2, p1_cat3, dataset[city]['labels'][x:x+size, y:y+size]

def buildings_loader(dataset, x, y, size):
    return dataset['images'][:,:, x:x+size, y:y+size], dataset['labels'][x:x+size, y:y+size]

def accimage_loader(path):
    import accimage
    try:
        return accimage.Image(path)
    except IOError:
        # Potentially a decoding problem, fall back to PIL.Image
        return pil_loader(path)

def default_loader(path):
    from torchvision import get_image_backend
    if get_image_backend() == 'accimage':
        return accimage_loader(path)
    else:
        return pil_loader(path)

class ImagePreloader(data.Dataset):

    def __init__(self, root, csv_file, class_map, transform=None, target_transform=None,
                 loader=default_loader):

        r = csv.reader(open(csv_file, 'r'), delimiter=',')

        images_list = []

        for row in r:
            images_list.append([row[0],row[1]])


        random.shuffle(images_list)
        classes, class_to_idx = class_map.keys(), class_map
        imgs = make_dataset(root, images_list, class_to_idx)
        if len(imgs) == 0:
            raise(RuntimeError("Found 0 images in subfolders of: " + root + "\n"
                               "Supported image extensions are: " + ",".join(IMG_EXTENSIONS)))

        self.root = root
        self.imgs = imgs
        self.classes = classes
        self.class_to_idx = class_to_idx
        self.transform = transform
        self.target_transform = target_transform
        self.loader = loader

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            tuple: (image, target) where target is class_index of the target class.
        """
        path, target = self.imgs[index]

        img = self.loader(path)
        if self.transform is not None:
            img = self.transform(img)
        if self.target_transform is not None:
            target = self.target_transform(target)

        return img, target

    def __len__(self):
        return len(self.imgs)


class OneraPreloader(data.Dataset):

    def __init__(self, root, metadata, input_size, full_load, loader, aug=False):
        random.shuffle(metadata)

        self.full_load = full_load
        self.input_size = input_size
        self.root = root
        self.imgs = metadata
        self.loader = loader
        self.aug = aug

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            tuple: (image, target) where target is class_index of the target class.
        """
        city, x, y = self.imgs[index]

        return self.loader(self.full_load, city, x, y, self.input_size, self.aug)

    def __len__(self):
        return len(self.imgs)

class BuildingsPreloader(data.Dataset):

    def __init__(self, root, csv_file, input_size, full_load, loader=buildings_loader):

        r = csv.reader(open(csv_file, 'r'), delimiter=',')

        images_list = []

        for row in r:
            images_list.append([int(row[0]), int(row[1])])

        random.shuffle(images_list)

        self.full_load = full_load
        self.input_size = input_size
        self.root = root
        self.imgs = images_list
        self.loader = loader

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            tuple: (image, target) where target is class_index of the target class.
        """
        x, y = self.imgs[index]

        img, target = self.loader(self.full_load, x, y, self.input_size)
#         print (img.shape)
        return img, target

    def __len__(self):
        return len(self.imgs)
