# 라이브러리 및 모듈 import
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
import numpy as np
import cv2
import os
import torch
from torch.utils.data import DataLoader, Dataset
import albumentations
import albumentations as A
from albumentations.pytorch.transforms import ToTensorV2
from effdet import get_efficientdet_config, EfficientDet, DetBenchTrain
from effdet.efficientdet import HeadNet
import pandas as pd
from tqdm import tqdm


# CustomDataset class 선언

class CustomDataset(Dataset):
    '''
      data_dir: data가 존재하는 폴더 경로
      transforms: data transform (resize, crop, Totensor, etc,,,)
    '''

    def __init__(self, annotation, data_dir, transforms=None):
        super().__init__()
        self.data_dir = data_dir
        
        # coco annotation 불러오기 (by. coco API)
        self.coco = COCO(annotation)
        self.predictions = {
            "images": self.coco.dataset["images"].copy(),
            "categories": self.coco.dataset["categories"].copy(),
            "annotations": None
        }
        self.transforms = transforms

    def __getitem__(self, index: int):
        image_id = self.coco.getImgIds(imgIds=index)

        image_info = self.coco.loadImgs(image_id)[0]
        
        image = cv2.imread(os.path.join(self.data_dir, image_info['file_name']))
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32)
        image /= 255.0

        ann_ids = self.coco.getAnnIds(imgIds=image_info['id'])
        anns = self.coco.loadAnns(ann_ids)

        # boxes (x, y, w, h)
        boxes = np.array([x['bbox'] for x in anns])

        # (x,y,w,h)의 coco format에서 (x_min, y_min, x_max, y_max)로 변경
        boxes[:, 2] = boxes[:, 0] + boxes[:, 2]
        boxes[:, 3] = boxes[:, 1] + boxes[:, 3]
        
        # box별 label
        labels = np.array([x['category_id'] for x in anns])
        labels = torch.as_tensor(labels, dtype=torch.int64)
        
        areas = np.array([x['area'] for x in anns])
        areas = torch.as_tensor(areas, dtype=torch.float32)
        
        is_crowds = np.array([x['iscrowd'] for x in anns])
        is_crowds = torch.as_tensor(is_crowds, dtype=torch.int64)

        target = {'boxes': boxes, 'labels': labels, 'image_id': torch.tensor([index]), 'area': areas,
                  'iscrowd': is_crowds}

        # transform
        if self.transforms:
            while True:
                sample = self.transforms(**{
                    'image': image,
                    'bboxes': target['boxes'],
                    'labels': labels
                })
                if len(sample['bboxes']) > 0:
                    image = sample['image']
                    target['boxes'] = torch.stack(tuple(map(torch.tensor, zip(*sample['bboxes'])))).permute(1, 0)
                    target['boxes'][:,[0,1,2,3]] = target['boxes'][:,[1,0,3,2]]  #yxyx: be warning
                    target['labels'] = torch.tensor(sample['labels'])
                    break
            
        return image, target, image_id
    
    def __len__(self) -> int:
        return len(self.coco.getImgIds())

# Albumentation을 이용, augmentation 선언
def get_train_transform():
    return A.Compose([
        A.Resize(1024, 1024),
        A.Flip(p=0.5),
        A.OneOf([A.HueSaturationValue(hue_shift_limit=0.2, sat_shift_limit= 0.2, val_shift_limit=0.2, p=0.9),
                               
                      A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.9)],p=0.9),
                      
                      A.ToGray(p=0.01),
                      
                      A.HorizontalFlip(p=0.5),
                      
                      A.VerticalFlip(p=0.5),
                      
                      A.Cutout(num_holes=8, max_h_size=64, max_w_size=64, fill_value=0, p=0.5),
                      
                      ToTensorV2(p=1.0)],
                      
                      p=1.0,
                        
                      bbox_params={'format': 'pascal_voc', 'label_fields': ['labels']})


def get_valid_transform():
    return A.Compose([
        ToTensorV2(p=1.0)
    ], bbox_params={'format': 'pascal_voc', 'label_fields': ['labels']})

# loss 추적
class Averager:
    def __init__(self):
        self.current_total = 0.0
        self.iterations = 0.0

    def send(self, value):
        self.current_total += value
        self.iterations += 1

    @property
    def value(self):
        if self.iterations == 0:
            return 0
        else:
            return 1.0 * self.current_total / self.iterations

    def reset(self):
        self.current_total = 0.0
        self.iterations = 0.0

def collate_fn(batch):
    return tuple(zip(*batch))

# Effdet config
# https://github.com/rwightman/efficientdet-pytorch/blob/master/effdet/config/model_config.py

# Effdet config를 통해 모델 불러오기
def get_net(checkpoint_path=None):
    
    config = get_efficientdet_config('efficientdetv2_ds')
    config.num_classes = 10
    config.image_size = (1024,1024)
    
    config.soft_nms = False
    config.max_det_per_image = 25
    
    net = EfficientDet(config, pretrained_backbone=True)
    net.class_net = HeadNet(config, num_outputs=config.num_classes)
    
    if checkpoint_path:
        checkpoint = torch.load(checkpoint_path)
        net.load_state_dict(checkpoint['model_state_dict'])
        
    return DetBenchTrain(net)
    
# train function
def train_fn(num_epochs, train_data_loader, optimizer, model, device, clip=35):
    loss_hist = Averager()
    model.train()
    
    for epoch in range(num_epochs):
        loss_hist.reset()
        
        for images, targets, image_ids in tqdm(train_data_loader):
            
                images = torch.stack(images) # bs, ch, w, h - 16, 3, 512, 512
                images = images.to(device).float()
                boxes = [target['boxes'].to(device).float() for target in targets]
                labels = [target['labels'].to(device).float() for target in targets]
                target = {"bbox": boxes, "cls": labels}

                # calculate loss
                loss, cls_loss, box_loss = model(images, target).values()
                loss_value = loss.detach().item()
                
                loss_hist.send(loss_value)
                
                # backward
                optimizer.zero_grad()
                loss.backward()
                # grad clip
                torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
                
                optimizer.step()

        print(f"Epoch #{epoch+1} loss: {loss_hist.value}")
        torch.save(model.state_dict(), f'epoch_{epoch+1}.pth')
    return loss_hist


def main():
    train_annotation = '/opt/ml/detection/dataset/train.json'
    val_annotation = '/opt/ml/detection/dataset/fold_4_val.json'
    data_dir = '/opt/ml/detection/dataset'

    train_dataset = CustomDataset(train_annotation, data_dir, get_train_transform())

    train_data_loader = DataLoader(
        train_dataset,
        batch_size=4,
        shuffle=False,
        num_workers=4,
        collate_fn=collate_fn
    )
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    print(device)

    model = get_net()
    model.to(device)
    
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params, lr=0.005, momentum=0.9, weight_decay=0.0005)

    num_epochs = 50

    loss = train_fn(num_epochs, train_data_loader, optimizer, model, device)

if __name__ == '__main__':
    main()