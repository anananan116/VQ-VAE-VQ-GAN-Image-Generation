import torch
from torch.optim import Adagrad
from torch.optim.lr_scheduler import CosineAnnealingLR
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
import os

def vae_loss(recon_x, x, z, z_q, beta=0.2):
    rec_loss = torch.nn.functional.mse_loss(recon_x, x, reduction='mean')
    quantization_loss = torch.nn.functional.mse_loss(z.detach(), z_q, reduction='mean') + beta * torch.nn.functional.mse_loss(z, z_q.detach(), reduction='mean')
    return rec_loss + quantization_loss, rec_loss, quantization_loss

def vae2_loss(recon_x, x, quant_loss, beta=0.25):
    rec_loss = torch.nn.functional.mse_loss(recon_x, x, reduction='mean')
    quant_loss = quant_loss * beta
    return rec_loss + quant_loss, rec_loss, quant_loss

class VQVAE_Trainer():
    def __init__(self, model, config):
        self.model = model.to(config['device'])
        self.optimizer = Adagrad(model.parameters(), lr=config['lr'])
        self.device = config['device']
        self.count_low_usage = config['count_low_usage']
        self.best_loss = float('inf')
        self.early_stop_patience = config['early_stop_patience']
        self.patience_counter = 0
        self.epochs = config['epochs']
        self.lr = config['lr']
        self.beta = config['beta']
        self.exp_id = config['experiment_id']
        self.writer = SummaryWriter(log_dir=f'./logs/exp{self.exp_id}/')
        self.version = config['version']
        print(model)

    def train(self, train_loader, validation_loader):
        self.scheduler = CosineAnnealingLR(self.optimizer, T_max=self.epochs * len(train_loader), eta_min=self.lr/10)
        for epoch in range(self.epochs):
            self.model.train()
            train_loss = 0.0
            train_rec_loss = 0.0
            train_quantization_loss = 0.0
            batch_num = 0
            progress_bar = tqdm(train_loader)
            count = []
            for data in train_loader:
                batch_num += 1
                data = data.to(self.device)
                self.optimizer.zero_grad()
                recon_x, quant_loss = self.model(data)
                loss, rec_loss, quantization_loss = vae2_loss(recon_x, data, quant_loss, beta=self.beta)
                loss.backward()
                self.optimizer.step()
                self.scheduler.step()
                train_loss += loss.item()
                train_rec_loss += rec_loss.item()
                train_quantization_loss += quantization_loss.item()
                if self.version == 1:
                    count.append((self.model.quantization.cluster_size > self.model.quantization.pixels_each_batch/self.model.quantization.n_embed/16).sum().item())
                else:
                    top_limit = self.model.quantization_top.pixels_each_batch/self.model.quantization_top.n_embed/self.model.quantization_top.lower_bound_factor
                    bottom_limit = self.model.quantization_bottom.pixels_each_batch/self.model.quantization_bottom.n_embed/self.model.quantization_bottom.lower_bound_factor
                    count.append((self.model.quantization_top.cluster_size > top_limit).sum().item() + (self.model.quantization_bottom.cluster_size > bottom_limit).sum().item())
                progress_bar.set_description(f"Epoch {epoch+1}, Loss: {(train_loss/batch_num):.4f}, Rec: {(train_rec_loss/batch_num):.4f}, quant: {(train_quantization_loss/batch_num):.4f}, count: {(sum(count)/len(count)):.4f}")
                progress_bar.update(1)
            progress_bar.close()
            avg_train_loss = train_loss / len(train_loader)
            avg_rec_loss = train_rec_loss / len(train_loader)
            avg_quantization_loss = train_quantization_loss / len(train_loader)
            self.writer.add_scalar('Epoch/train_loss', avg_train_loss, epoch)
            self.writer.add_scalar('Epoch/train_rec_loss', avg_rec_loss, epoch)
            self.writer.add_scalar('Epoch/train_quantization_loss', avg_quantization_loss, epoch)

            val_loss = self.validate(validation_loader, epoch)
            if val_loss < self.best_loss:
                self.best_loss = val_loss
                self.save_model(f'./results/best_model_exp{self.exp_id}.pth')
                self.patience_counter = 0
            else:
                self.patience_counter += 1
                if self.patience_counter > self.early_stop_patience:
                    print("Early stopping triggered.")
                    break

            if epoch % 5 == 0:
                self.save_model(f'./results/model_epoch_{epoch}_exp{self.exp_id}.pth')

    def validate(self, validation_loader, epoch):
        self.model.eval()
        val_loss = 0.0
        val_rec_loss = 0.0
        val_quantization_loss = 0.0
        batch_num = 0
        with torch.no_grad():
            for data in validation_loader:
                batch_num += 1
                data = data.to(self.device)
                recon_x, quant_loss = self.model(data)
                loss, rec_loss, quantization_loss = vae2_loss(recon_x, data, quant_loss, beta=self.beta)
                val_loss += loss.item()
                val_rec_loss += rec_loss.item()
                val_quantization_loss += quantization_loss.item()
                # Log validation losses

        avg_val_loss = val_loss / len(validation_loader)
        avg_rec_loss = val_rec_loss / len(validation_loader)
        self.writer.add_scalar('Epoch/val_loss', avg_val_loss, epoch)
        avg_quantization_loss = val_quantization_loss / len(validation_loader)
        self.writer.add_scalar('Epoch/val_rec_loss', avg_rec_loss, epoch)
        self.writer.add_scalar('Epoch/val_quantization_loss', avg_quantization_loss, epoch)
        print(f'Epoch {epoch+1}, Val Loss: {avg_val_loss:.4f}, Rec: {avg_rec_loss:.4f}, Quant: {avg_quantization_loss:.4f}')
        return avg_rec_loss
    def save_model(self, path):
        if isinstance(self.model, torch.nn.DataParallel):
            # Save the original model which is accessible via model.module
            torch.save(self.model.module.state_dict(), path)
        else:
            # Save the model directly if not using DataParallel
            torch.save(self.model.state_dict(), path)