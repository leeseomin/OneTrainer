import os
from typing import Iterator

import torch
from diffusers import AutoencoderKL, UNet2DConditionModel, DDPMScheduler, StableDiffusionDepth2ImgPipeline, StableDiffusionInpaintPipeline, StableDiffusionPipeline, DiffusionPipeline
from torch import Tensor
from torch.nn import Parameter
from torch.optim import Optimizer
from torchvision.transforms import transforms
from transformers import CLIPTextModel, CLIPTokenizer, DPTImageProcessor, DPTForDepthEstimation

from modules.model.BaseModel import BaseModel
from modules.util.TrainProgress import TrainProgress
from modules.util.args.TrainArgs import TrainArgs
from modules.util.enum.ModelType import ModelType


class StableDiffusionModel(BaseModel):
    # base model data
    model_type: ModelType
    tokenizer: CLIPTokenizer
    noise_scheduler: DDPMScheduler
    text_encoder: CLIPTextModel
    vae: AutoencoderKL
    unet: UNet2DConditionModel
    image_depth_processor: DPTImageProcessor
    depth_estimator: DPTForDepthEstimation

    # persistent training data
    optimizer: Optimizer | None
    optimizer_state_dict: dict | None
    train_progress: TrainProgress

    def __init__(
            self,
            model_type: ModelType,
            tokenizer: CLIPTokenizer,
            noise_scheduler: DDPMScheduler,
            text_encoder: CLIPTextModel,
            vae: AutoencoderKL,
            unet: UNet2DConditionModel,
            image_depth_processor: DPTImageProcessor | None = None,
            depth_estimator: DPTForDepthEstimation | None = None,
            optimizer_state_dict: dict | None = None,
            train_progress: TrainProgress = TrainProgress()
    ):
        super(StableDiffusionModel, self).__init__(model_type)

        self.tokenizer = tokenizer
        self.noise_scheduler = noise_scheduler
        self.text_encoder = text_encoder
        self.vae = vae
        self.unet = unet
        self.image_depth_processor = image_depth_processor
        self.depth_estimator = depth_estimator

        self.optimizer = None
        self.optimizer_state_dict = optimizer_state_dict
        self.train_progress = train_progress

    def parameters(self, args: TrainArgs) -> Iterator[Parameter]:
        if args.train_text_encoder:
            return list(self.text_encoder.parameters()) + list(self.unet.parameters())
        else:
            return list(self.unet.parameters())

    @staticmethod
    def __save_image(image_tensor: Tensor, directory: str, name: str, step: int):
        path = os.path.join(directory, "step-" + str(step) + "-" + name + ".png")
        if not os.path.exists(directory):
            os.makedirs(directory)

        t = transforms.Compose([
            transforms.ToPILImage(),
        ])

        image_tensor = image_tensor[0].unsqueeze(0)

        range_min = -1
        range_max = 1
        image_tensor = (image_tensor - range_min) / (range_max - range_min)

        image = t(image_tensor.squeeze())
        image.save(path)

    def predict(self, batch: dict, args: TrainArgs) -> (Tensor, Tensor):
        latent_image = batch['latent_image']
        scaled_latent_image = latent_image * self.vae.scaling_factor

        latent_conditioning_image = None
        scaled_latent_conditioning_image = None
        if args.model_type.has_conditioning_image_input():
            latent_conditioning_image = batch['latent_conditioning_image']
            scaled_latent_conditioning_image = latent_conditioning_image * self.vae.scaling_factor

        generator = torch.Generator(device=args.train_device)
        generator.manual_seed(self.train_progress.global_step)

        if args.offset_noise_weight > 0:
            normal_noise = torch.randn(scaled_latent_image.shape, generator=generator, device=args.train_device, dtype=args.train_dtype)
            offset_noise = torch.randn(scaled_latent_image.shape[0], scaled_latent_image.shape[1], 1, 1, generator=generator, device=args.train_device, dtype=args.train_dtype)
            latent_noise = normal_noise + (args.offset_noise_weight * offset_noise)
        else:
            latent_noise = torch.randn(scaled_latent_image.shape, generator=generator, device=args.train_device, dtype=args.train_dtype)

        timestep = torch.randint(
            low=0,
            high=int(self.noise_scheduler.config['num_train_timesteps'] * args.max_noising_strength),
            size=(scaled_latent_image.shape[0],),
            device=scaled_latent_image.device,
        ).long()

        scaled_noisy_latent_image = self.noise_scheduler.add_noise(original_samples=scaled_latent_image, noise=latent_noise, timesteps=timestep)

        text_encoder_output = self.text_encoder(batch['tokens'], return_dict=True)[0]

        if args.model_type.has_mask_input() and args.model_type.has_conditioning_image_input():
            latent_input = torch.concat([scaled_noisy_latent_image, batch['latent_mask'], scaled_latent_conditioning_image], 1)
        else:
            latent_input = scaled_noisy_latent_image

        if args.model_type.has_depth_input():
            predicted_latent_noise = self.unet(latent_input, timestep, text_encoder_output, batch['latent_depth']).sample
        else:
            predicted_latent_noise = self.unet(latent_input, timestep, text_encoder_output).sample

        if args.debug_mode:
            with torch.no_grad():
                # noise
                noise = self.vae.decode(latent_noise / self.vae.scaling_factor).sample
                noise = noise.clamp(-1, 1)
                self.__save_image(noise, args.debug_dir + "/training_batches", "1-noise", self.train_progress.global_step)

                # predicted noise
                predicted_noise = self.vae.decode(predicted_latent_noise / self.vae.scaling_factor).sample
                predicted_noise = predicted_noise.clamp(-1, 1)
                self.__save_image(predicted_noise, args.debug_dir + "/training_batches", "2-predicted_noise", self.train_progress.global_step)

                # noisy image
                noisy_latent_image = scaled_noisy_latent_image / self.vae.scaling_factor
                noisy_image = self.vae.decode(noisy_latent_image).sample
                noisy_image = noisy_image.clamp(-1, 1)
                self.__save_image(noisy_image, args.debug_dir + "/training_batches", "3-noisy_image", self.train_progress.global_step)

                # predicted image
                sqrt_alpha_prod = self.noise_scheduler.alphas_cumprod[timestep] ** 0.5
                sqrt_alpha_prod = sqrt_alpha_prod.flatten().reshape(-1, 1, 1, 1)

                sqrt_one_minus_alpha_prod = (1 - self.noise_scheduler.alphas_cumprod[timestep]) ** 0.5
                sqrt_one_minus_alpha_prod = sqrt_one_minus_alpha_prod.flatten().reshape(-1, 1, 1, 1)

                scaled_predicted_latent_image = (scaled_noisy_latent_image - predicted_latent_noise * sqrt_one_minus_alpha_prod) / sqrt_alpha_prod
                predicted_latent_image = scaled_predicted_latent_image / self.vae.scaling_factor
                predicted_image = self.vae.decode(predicted_latent_image).sample
                predicted_image = predicted_image.clamp(-1, 1)
                self.__save_image(predicted_image, args.debug_dir + "/training_batches", "4-predicted_image", self.train_progress.global_step)

                # image
                image = self.vae.decode(latent_image).sample
                image = image.clamp(-1, 1)
                self.__save_image(image, args.debug_dir + "/training_batches", "5-image", self.train_progress.global_step)

                # conditioning image
                if self.model_type.has_conditioning_image_input():
                    conditioning_image = self.vae.decode(latent_conditioning_image).sample
                    conditioning_image = conditioning_image.clamp(-1, 1)
                    self.__save_image(conditioning_image, args.debug_dir + "/training_batches", "6-conditioning_image", self.train_progress.global_step)

        return predicted_latent_noise, latent_noise

    def create_pipeline(self) -> DiffusionPipeline:
        if self.model_type.has_depth_input():
            return StableDiffusionDepth2ImgPipeline(
                vae=self.vae,
                text_encoder=self.text_encoder,
                tokenizer=self.tokenizer,
                unet=self.unet,
                scheduler=self.noise_scheduler,
                depth_estimator=self.depth_estimator,
                feature_extractor=self.image_depth_processor,
            )
        elif self.model_type.has_conditioning_image_input():
            return StableDiffusionInpaintPipeline(
                vae=self.vae,
                text_encoder=self.text_encoder,
                tokenizer=self.tokenizer,
                unet=self.unet,
                scheduler=self.noise_scheduler,
                safety_checker=None,
                feature_extractor=None,
                requires_safety_checker=False,
            )
        else:
            return StableDiffusionPipeline(
                vae=self.vae,
                text_encoder=self.text_encoder,
                tokenizer=self.tokenizer,
                unet=self.unet,
                scheduler=self.noise_scheduler,
                safety_checker=None,
                feature_extractor=None,
                requires_safety_checker=False,
            )
