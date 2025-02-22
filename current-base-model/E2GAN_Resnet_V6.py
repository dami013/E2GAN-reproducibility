import torch
import torch.nn as nn
import torchvision.transforms as transforms
from torchvision.models import resnet50, ResNet50_Weights
from torch.utils.data import Dataset, DataLoader, random_split
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.nn.utils import spectral_norm
from PIL import Image
import os
from torchvision.models import resnet50, ResNet50_Weights
from torchvision.models.resnet import BasicBlock
from transformers import CLIPTextModel, CLIPTokenizer
import numpy as np
from tqdm import tqdm
import time
from torch.utils.data import ConcatDataset
import torch_fidelity

# Gaussian Noise Layer
class GaussianNoise(nn.Module):
    def __init__(self, sigma=0.1):
        super().__init__()
        self.sigma = sigma
    
    def forward(self, x):
        if self.training:
            noise = torch.randn_like(x) * self.sigma
            return x + noise
        return x

# Discriminator
class Discriminator(nn.Module):
    def __init__(self, input_channels=3):
        super().__init__()
        self.model = nn.Sequential(
            nn.Conv2d(input_channels, 64, 4, stride=2, padding=1),
            nn.LeakyReLU(0.2),
            nn.Conv2d(64, 128, 4, stride=2, padding=1),
            nn.InstanceNorm2d(128),
            nn.LeakyReLU(0.2),
            nn.Conv2d(128, 256, 4, stride=2, padding=1),
            nn.InstanceNorm2d(256),
            nn.LeakyReLU(0.2),
            nn.Conv2d(256, 512, 4, stride=2, padding=1),
            nn.InstanceNorm2d(512),
            nn.LeakyReLU(0.2),
            nn.Conv2d(512, 1, 4, stride=1, padding=1)
        )
    
    def forward(self, x):
        return self.model(x)
class TextGuidedGenerator(nn.Module):
    def __init__(self, input_channels=3, output_channels=3, text_embedding_dim=512, dropout_rate=0.1, device='cuda'):
        super().__init__()
        
        # CLIP text encoder
        self.clip_model = CLIPTextModel.from_pretrained("openai/clip-vit-base-patch32").to(device)
        self.tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-base-patch32")
        
        # Freeze CLIP parameters
        for param in self.clip_model.parameters():
            param.requires_grad = False
            param.requires_grad_(False)
            
        # 1. Downsampling iniziale + primi due blocchi usando ResNet50
        resnet = resnet50(weights=ResNet50_Weights.IMAGENET1K_V2)
        self.downsample_resnet = nn.Sequential(
            resnet.conv1,      # Primo downsampling 224 -> 112
            resnet.bn1,
            resnet.relu,
            resnet.maxpool,    # Altro downsampling 112 -> 56
            resnet.layer1,     # Primo ResNet block
            resnet.layer2      # Secondo ResNet block
        )
        for param in self.downsample_resnet.parameters():
            param.requires_grad = False
        
        # 3. DS prima del transformer (56 -> 28)
        self.pre_transformer_ds = nn.Conv2d(512 + text_embedding_dim, 256, 3, stride=2, padding=1)
        
        # 4. Transformer block
        self.transformer = nn.Transformer(
            d_model=256,
            nhead=8,
            num_encoder_layers=3,
            num_decoder_layers=3,
            dim_feedforward=1024,
            dropout=0.1,
            activation='gelu',
            batch_first=True
        )
        
              # 5. US dopo transformer (28 -> 56)
        self.post_transformer_us = nn.Sequential(
            spectral_norm(nn.ConvTranspose2d(256, 128, 4, stride=2, padding=1)),
            nn.BatchNorm2d(128),
            nn.LeakyReLU(0.2)
        )
        
        # 6. ResNet block post-transformer (mantiene 56)
        self.post_transformer_resnet = BasicBlock(128, 128)
        
       # 7. Upsampling finale (28 -> 224) con tre upsampling steps
        self.final_upsample = nn.Sequential(
            # Primo upsampling 28 -> 56
            nn.ConvTranspose2d(128, 128, 4, stride=2, padding=1),
            nn.BatchNorm2d(128),
            nn.LeakyReLU(0.2),
            # Secondo upsampling 56 -> 112
            nn.ConvTranspose2d(128, 64, 4, stride=2, padding=1),
            nn.BatchNorm2d(64),
            nn.LeakyReLU(0.2),
            # Terzo upsampling 112 -> 224
            nn.ConvTranspose2d(64, 32, 4, stride=2, padding=1),
            nn.BatchNorm2d(32),
            nn.LeakyReLU(0.2),
            # Conv finale
            nn.Conv2d(32, output_channels, 7, padding=3),
            nn.Tanh()
        )
        
    def encode_text(self, text):
        # Get the device from CLIP model
        device = self.clip_model.device
        # Tokenize and encode text using CLIP
        tokens = self.tokenizer(text, padding=True, truncation=True, return_tensors="pt").to(device)
        text_features = self.clip_model(**tokens).last_hidden_state.mean(dim=1)  # Average pooling
        return text_features
    
    def forward(self, x, text_prompt):
        #print(f"Input shape: {x.shape}")
        batch_size = x.size(0)
        
        # Text embedding
        if isinstance(text_prompt, (list, tuple)):
            text_embedding = self.encode_text(text_prompt)
        else:
            text_embedding = self.encode_text([text_prompt] * batch_size)
        #print(f"Text embedding shape: {text_embedding.shape}")
            
        # 1&2. Downsample e ResNet blocks usando ResNet50 (224 -> 56)
        x = self.downsample_resnet(x)
        #print(f"After downsample_resnet shape: {x.shape}")
        
        # 3. Concatenate text embedding e DS
        text_embedding = text_embedding.unsqueeze(-1).unsqueeze(-1)
        text_embedding = text_embedding.expand(-1, -1, x.size(2), x.size(3))
        #print(f"Expanded text embedding shape: {text_embedding.shape}")
        
        x = torch.cat([x, text_embedding], dim=1)
        #print(f"After concatenation shape: {x.shape}")
        
        x = self.pre_transformer_ds(x)  # 56 -> 28
        #print(f"After pre_transformer_ds shape: {x.shape}")
        
        # 4. Transformer
        b, c, h, w = x.shape
        x = x.view(b, c, h*w).permute(0, 2, 1)
        #print(f"Before transformer shape: {x.shape}")
        
        x = self.transformer(x, x)
        #print(f"After transformer shape: {x.shape}")
        
        x = x.permute(0, 2, 1).view(b, c, h, w)
        #print(f"After reshape back to spatial shape: {x.shape}")
        
        # 5. Post-transformer US (28 -> 56)
        x = self.post_transformer_us(x)
        #print(f"After post_transformer_us shape: {x.shape}")
        
        # 6. Post-transformer ResNet block
        x = self.post_transformer_resnet(x)
        #print(f"After post_transformer_resnet shape: {x.shape}")
        
        # 7. Final upsampling (56 -> 224)
        x = self.final_upsample(x)
        #print(f"Final output shape: {x.shape}")
        
        return x
class TextGuidedImageDataset(Dataset):
    def __init__(self, source_dir, target_dir, text_prompt, transform=None):
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.text_prompt = text_prompt
        self.transform = transform
        source_images = set(os.listdir(source_dir))
        target_images = set(os.listdir(target_dir))
        self.images = list(source_images.intersection(target_images))
        if len(self.images) == 0:
            raise ValueError("No matching images found.")
        print(f"Found {len(self.images)} matching images for prompt: {text_prompt}")
    
    def __len__(self):
        return len(self.images)
    
    def __getitem__(self, idx):
        img_name = self.images[idx]
        source_path = os.path.join(self.source_dir, img_name)
        target_path = os.path.join(self.target_dir, img_name)
        source_image = Image.open(source_path).convert('RGB')
        target_image = Image.open(target_path).convert('RGB')
        if self.transform:
            source_image = self.transform(source_image)
            target_image = self.transform(target_image)
        # Ensure text prompt is passed as string
        return source_image, target_image, str(self.text_prompt)

def calculate_fid(real_images_dir, generated_images_dir, batch_size=50, device='cuda'):
    """Calculates the FID score between real and generated images."""
    metrics_dict = torch_fidelity.calculate_metrics(
        input1=real_images_dir,
        input2=generated_images_dir,
        batch_size=batch_size,
        cuda=True if device == 'cuda' else False,
        isc=False,
        fid=True,
    )
    return metrics_dict['frechet_inception_distance']

def train_text_guided_gan(generator, discriminator, train_loader, val_loader, num_epochs, device, save_dir="models"):
    os.makedirs(save_dir, exist_ok=True)
    criterion_gan = nn.MSELoss()
    criterion_pixel = nn.L1Loss()
    optimizer_g = torch.optim.AdamW(generator.parameters(), lr=0.0003, betas=(0.5, 0.999), weight_decay=1e-1)
    optimizer_d = torch.optim.AdamW(discriminator.parameters(), lr=0.0001, betas=(0.5, 0.999), weight_decay=1e-1)
    scheduler_g = CosineAnnealingLR(optimizer_g, T_max=num_epochs, eta_min=1e-6)
    scheduler_d = CosineAnnealingLR(optimizer_d, T_max=num_epochs, eta_min=1e-6)
    best_val_loss = float('inf')
    patience = 10
    early_stop_counter = 0
    train_metrics = {'g_loss': [], 'd_loss': [], 'val_loss': []}
    
    for epoch in range(num_epochs):
        generator.train()
        discriminator.train()
        total_train_loss = 0
        
        for source, target, text_prompt in tqdm(train_loader):
            batch_size = source.size(0)
            real = target.to(device)
            source = source.to(device)
            # Ensure text_prompt is a list of strings
            if isinstance(text_prompt, torch.Tensor):
                text_prompt = text_prompt.tolist()
            
            # Train discriminator
            optimizer_d.zero_grad()
            fake = generator(source, text_prompt)
            pred_real = discriminator(real)
            pred_fake = discriminator(fake.detach())
            real_labels = torch.ones_like(pred_real) * 0.9
            fake_labels = torch.zeros_like(pred_fake) * 0.1
            loss_d_real = criterion_gan(pred_real, real_labels)
            loss_d_fake = criterion_gan(pred_fake, fake_labels)
            loss_d = (loss_d_real + loss_d_fake) * 0.5
            loss_d.backward()
            optimizer_d.step()
            
            # Train generator
            optimizer_g.zero_grad()
            pred_fake = discriminator(fake)
            loss_g_gan = criterion_gan(pred_fake, torch.ones_like(pred_fake))
            loss_g_pixel = criterion_pixel(fake, real) * 20
            loss_g = loss_g_gan + loss_g_pixel
            loss_g.backward()
            optimizer_g.step()
            
            train_metrics['g_loss'].append(loss_g.item())
            train_metrics['d_loss'].append(loss_d.item())
        
        # Validation
        generator.eval()
        total_val_loss = 0
        with torch.no_grad():
            for source, target, text_prompt in val_loader:
                source = source.to(device)
                target = target.to(device)
                fake = generator(source, text_prompt)
                val_loss = criterion_pixel(fake, target).item()
                total_val_loss += val_loss
        
        avg_val_loss = total_val_loss / len(val_loader)
        train_metrics['val_loss'].append(avg_val_loss)
        
        print(f"Epoch {epoch+1}/{num_epochs}: G_loss={np.mean(train_metrics['g_loss'][-len(train_loader):]):.4f}, "
              f"D_loss={np.mean(train_metrics['d_loss'][-len(train_loader):]):.4f}, Val_loss={avg_val_loss:.4f}")
        
        scheduler_g.step()
        scheduler_d.step()
    
    # Save final model
    torch.save({
        'generator': generator.state_dict(),
        'discriminator': discriminator.state_dict(),
        'epoch': num_epochs-1,
        'train_metrics': train_metrics
    }, os.path.join(save_dir, "final_model.pth"))
    
    return train_metrics

if __name__ == '__main__':
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
    ])

    # Define source directory and text-guided target directories with prompts
    source_dir = '/kaggle/input/multiprompt/original_images_ordered'
    style_configs = [
        ('/kaggle/input/multiprompt/albino', "convert to albino style"),
        ('/kaggle/input/multiprompt/blonde', "make the hair blonde"),
        ('/kaggle/input/multiprompt/gogh', "apply van gogh style")
    ]
    
    # Create datasets with text prompts
    datasets = []
    for target_dir, prompt in style_configs:
        dataset = TextGuidedImageDataset(source_dir, target_dir, prompt, transform)
        datasets.append(dataset)

    full_dataset = ConcatDataset(datasets)
    print(f"Combined dataset contains {len(full_dataset)} images")
    
    train_size = int(0.8 * len(full_dataset))
    val_size = len(full_dataset) - train_size
    train_dataset, val_dataset = random_split(full_dataset, [train_size, val_size])
    
    # Create data loaders
    train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True, num_workers=4)
    val_loader = DataLoader(val_dataset, batch_size=32, shuffle=False, num_workers=4)
    
    # Initialize models
    generator = TextGuidedGenerator(device=device).to(device)
    discriminator = Discriminator().to(device)
    

    
    # Train the model
    start_time = time.time()
    train_metrics = train_text_guided_gan(
        generator, 
        discriminator, 
        train_loader, 
        val_loader, 
        num_epochs=100, 
        device=device
    )
    end_time = (time.time() - start_time)/60
    print(f"Training completed in {end_time:.2f} minutes")
