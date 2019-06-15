import sys
#sys.path.append("./devito")

import argparse
import logging
import os

import numpy as np
from sklearn.metrics import accuracy_score

import torch
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter

from seisgan.fwi import layers
from seisgan.networks import GeneratorMultiChannel, HalfChannels, DiscriminatorUpsampling, HalfChannelsTest
from seisgan.optimizers import MALA, SGHMC
from seisgan.utils import set_seed, make_dir
from seisgan.tensorboard_utils import add_model_to_writer, add_seismic_to_writer


# Writer will output to ./runs/ directory by default
parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=0, help="Random Seed")
parser.add_argument("--run_name", type=str, default="test", help="Set Run Name")
parser.add_argument("--test_image_id", type=int, default=0, help="Which test image to use as gt")
parser.add_argument("--num_runs", type=int, default=1, help="How many inversions to perform")

parser.add_argument("--working_dir", type=str, default="./", help="Set working directory")
parser.add_argument("--out_folder", type=str, default="./", help="Output folder")
parser.add_argument("--discriminator_path", type=str, default="./", help="Output folder")
parser.add_argument("--generator_path", type=str, default="./", help="Output folder")
parser.add_argument("--minsmaxs_path", type=str, default="./", help="Output folder")
parser.add_argument("--testimgs_path", type=str, default="./", help="Output folder")

parser.add_argument("--store_gt_waveform", action="store_true", help="Store GT Waveform")
parser.add_argument("--store_final_reconstruction_waveform", action="store_true", help="Store final inverted model waveform")

parser.add_argument("--sources", type=int, default=2, help="How many sources to use")
parser.add_argument("--wavelet_frequency", type=float, default=1e-2, help="Wavelet Peak Frequency")
parser.add_argument("--simulation_time", type=float, default=1e3, help="Shot recording time")
parser.add_argument("--top_padding", type=int, default=32, help="Pad GAN domain above")
parser.add_argument("--bottom_padding", type=int, default=32, help="Pad GAN domain below")
parser.add_argument("--noise_percent", type=float, default=0.02, help="Percent of noise to add to observed shot data")

parser.add_argument("--error_termination", action="store_true", help="Terminate only if error reached")
parser.add_argument("--seismic_relative_error", type=float, default=0.1, help="Target Relative Error")
parser.add_argument("--well_accuracy", type=float, default=0.95, help="Target Well Accuracy")
parser.add_argument("--use_disc", action="store_true", help="Use discriminator loss")
parser.add_argument("--use_well", action="store_true", help="Use well loss")

parser.add_argument("--learning_rate", type=float, default=1e-2, help="Learning Rate")
parser.add_argument("--final_learning_rate", type=float, default=0.00001, help="Final Learning Rate")
parser.add_argument("--weight_decay", type=float, default=1e-5, help="Weight Decay")
parser.add_argument("--max_iter", type=int, default=200, help="maximum number of iterations")
parser.add_argument("--lambda_perceptual", type=float, default=1.0, help="Weight of perceptual loss")
parser.add_argument("--lambda_fwi", type=float, default=1.0, help="Weight of fwi loss")
parser.add_argument("--lambda_well", type=float, default=100.0, help="Weight of well loss")

parser.add_argument("--tensorboard", action="store_true", help="Use tensorboard")
parser.add_argument("--print_to_console", action="store_true", help="Stdout to console")
args = parser.parse_args()

set_seed(args.seed)

working_dir = args.working_dir
out_folder = args.run_name
run_name = args.run_name+"_"+str(args.seed)

loss_names = ["Disc", "Facies", "Vp", "FWI"]
if args.use_well:
    loss_names = ["Disc", "Facies", "FWI"]
elif not args.use_well:
    loss_names = ["Disc", "FWI"]

log_path = os.path.expandvars(working_dir+"/"+out_folder+"/"+"logs")

latent_variables_out_path = os.path.expandvars(working_dir+"/"+out_folder+"/"+run_name+"_latents_")
shots_out_path = os.path.expandvars(working_dir+"/"+out_folder+"/"+run_name+"_shots_")
losses_out_path = os.path.expandvars(working_dir+"/"+out_folder+"/"+run_name+"_losses_")

make_dir(log_path)
make_dir(os.path.expandvars(working_dir+"/"+out_folder))

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# create a file handler
handler = logging.FileHandler(filename=os.path.expandvars(working_dir+"/"+out_folder+"/"+"logs"+"/"+run_name+"_log.log"))
if args.print_to_console:
    handler = logging.StreamHandler(sys.stdout)

handler.setLevel(logging.INFO)

# create a logging format
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

# add the handlers to the logger
logger.addHandler(handler)

logger.info('Started Logging')

generator_path = os.path.expandvars(args.generator_path)
discriminator_path = os.path.expandvars(args.discriminator_path)
minsmaxs_path = os.path.expandvars(args.minsmaxs_path)
testimgs_path = os.path.expandvars(args.testimgs_path)

logger.info('Expanded Variables')

tn = lambda n: n.data.cpu().numpy()

configuration = {"t0": 0.,
                 "tn": args.simulation_time,
                 "shape": (128, args.bottom_padding+64+args.top_padding),
                 "nbpml": 50,
                 "origin": (0., 0.),
                 "spacing": (10., 1.),
                 "source_min_x": 3,
                 "source_min_y": 10,
                 "nshots": args.sources,
                 "f0": args.wavelet_frequency,
                 "nreceivers": 128,
                 "rec_min_x": 3,
                 "rec_min_y": 10,
                 "noise_percent": args.noise_percent
                 }

generator = GeneratorMultiChannel()
new_state_dict = torch.load(generator_path)
generator.load_state_dict(new_state_dict)
generator.cpu()
generator.eval()

discriminator = DiscriminatorUpsampling()
new_state_dict = torch.load(discriminator_path)
discriminator.load_state_dict(new_state_dict)
discriminator.cpu()
discriminator.eval()

logger.info('Loaded Networks')

minsmaxs = np.load(minsmaxs_path)

half_channel_gen = HalfChannels(generator, min_vp=minsmaxs[2, 1], max_vp=minsmaxs[3, 1], vp_bottom=1.8, vp_top=1.8, top_size=args.top_padding)
half_channel_test = HalfChannelsTest(min_vp=minsmaxs[2, 1], max_vp=minsmaxs[3, 1], vp_bottom=1.8, vp_top=1.8, top_size=args.top_padding)

gt_image = np.load(testimgs_path)[args.test_image_id]

x_gt = torch.from_numpy(gt_image).float().unsqueeze(0)
x_gt_facies = x_gt
with torch.no_grad():
    x_gt, x_geo_gt = half_channel_test(x_gt)

logger.info('Loaded GT facies')

fwi_model_config = layers.FWIConfiguration(configuration, tn(x_gt)[0, 0])
fwi_loss = layers.FWILoss(fwi_model_config)
gt_sum = np.sum([x_i.data for x_i in fwi_loss.true_ds], 0)

if args.store_gt_waveform:
    np.save(shots_out_path+"gt.npy", gt_sum)
logger.info('Defined Layers')

num_count = 0
lr = args.learning_rate
model_try = 0
while num_count < args.num_runs:
    seed = np.random.randint(low=0, high=2**31)
    set_seed(seed)
    logger.info('Started Model Inference: '+str(num_count))

    if args.tensorboard:
        writer = SummaryWriter(log_dir=os.path.expandvars(working_dir+"/"+"tensorboard"+"/run_"+str(int(seed))))

    z_star = torch.randn(1, 50, 1, 2)
    z_star.requires_grad = True

    optimizer = MALA([z_star], lr=args.learning_rate, weight_decay=args.weight_decay)#SGHMC([z_star], lr=config["optimization"]["learning_rate"], nu=0.1)#MALA([z_star], lr=config["optimization"]["learning_rate"], weight_decay=0.0)#SGD([z_star], lr=config["optimization"]["learning_rate"], weight_decay=1e-5) #Adam([z_star], lr=config["optimization"]["learning_rate"])
    losses = []
    zs = []
    shots = []
    losses_total = []
    pred_sum = None
    latent_diverged = False
    acc = 0.0
    for i in range(0, args.max_iter):
        loss_vars = []
        optimizer.zero_grad()
        x_star, x_geo = half_channel_gen(z_star)

        if args.use_disc:
            d = -args.lambda_perceptual*discriminator(x_geo).mean()
            loss_vars.append(d)

        if args.use_well:
            for channel, lambd, loss_type, transform, name in zip([0], [args.lambda_well],
                                                                  [torch.nn.functional.binary_cross_entropy],
                                                                  [layers.to_probability],
                                                                  ["Facies"]):
                for well in [64]:
                    well_loss = lambd * layers.well_loss_old(x_geo, x_gt_facies.float(), well, channel,
                                                         loss=loss_type, transform=transform)

                    acc = accuracy_score(tn(x_gt_facies)[:, 0, :, 64].flatten().astype(int),
                                         np.where(layers.to_probability(tn(x_geo)[:, 0, :, 64]).flatten() > 0.5, 1, 0))
                    print(acc)

                    well_loss.backward(retain_graph=True)
                    #if i < 20:
                    #    nn.utils.clip_grad_norm_(z_star, 10.0)
                    if args.tensorboard:
                        writer.add_scalar("well_acc", acc, global_step=i)
                        writer.add_scalar("well_loss", well_loss, global_step=i)
                        writer.add_scalar("well_grad_norm", z_star.grad.norm(), global_step=i)

        #if args.tensorboard:
        #    add_seismic_to_writer("seismic_noise", writer, fwi_loss.true_ds, i)

        l = args.lambda_fwi*fwi_loss(x_star)
        loss_vars.append(l)

        pred_sum = fwi_loss.smooth_ds

        #if args.tensorboard:
        #    add_seismic_to_writer("seismic_synth", writer, fwi_loss.smooth_ds, i, sum=False)

        print(x_star.size())

        #if args.tensorboard:
        #    add_model_to_writer("model", writer, x_geo[0, 0].detach().cpu().numpy(), i)

        rmse_noise = np.sqrt(np.mean((fwi_loss.noisy_ds-fwi_loss.clean_ds)**2))
        rmse_inversion = np.sqrt(np.mean((pred_sum-fwi_loss.clean_ds)**2))

        print(rmse_noise)
        print(rmse_inversion)
        print(rmse_inversion/rmse_noise)

        error = np.linalg.norm(pred_sum-gt_sum)
        rel_error = error/np.linalg.norm(gt_sum)

        print(np.linalg.norm(fwi_loss.noisy_ds-fwi_loss.clean_ds)/np.linalg.norm(fwi_loss.clean_ds))

        losses.append(rel_error)

        total_losses_sum = sum(loss_vars)
        total_losses_sum.backward()

        optimizer.step()

        zs.append(z_star.data.numpy().copy())

        for param_group in optimizer.param_groups:
            param_group['lr'] -= (args.learning_rate-args.final_learning_rate)/args.max_iter
            lr = param_group['lr']

        print(lr)

        print(z_star.std())

        if args.tensorboard:
            writer.add_scalar("lr", lr, global_step=i)
            writer.add_scalar("z_std", z_star.std(), global_step=i)
            writer.add_scalar("z_grad_norm", z_star.grad.norm(), global_step=i)
            writer.add_scalar("rel_error", rel_error, global_step=i)

        if z_star.std().data.numpy() > 5.0:
            logger.info(
                'NOT COMPLETED Optimization, Latent Space Diverged')
            latent_diverged = True
            model_try += 1
            break

        for ls, name in zip(loss_vars, loss_names):
            logger.info('Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + " " +name+': ' + str(tn(ls)))
        logger.info('Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + " " + "Relative Error" + ': ' + str(rel_error))
        if args.use_well:
            logger.info('Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + ' Accuracy: ' + str(acc))

    if not args.error_termination:
        if rel_error <= args.seismic_relative_error and (args.use_well and acc >= args.well_accuracy):
            logger.info('COMPLETED Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + ' Loss: ' + str(rel_error))

            zs_out = np.array(zs)
            np.save(latent_variables_out_path+str(num_count)+".npy", zs_out)
            if args.store_final_reconstruction_waveform:
                np.save(shots_out_path + str(num_count) + ".npy", pred_sum)
            np.save(losses_out_path+str(num_count)+".npy", np.array(losses))
            num_count += 1
            model_try += 1
            break

        elif rel_error <= args.seismic_relative_error and not args.use_well:
            logger.info('COMPLETED Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + ' Loss: ' + str(rel_error))

            zs_out = np.array(zs)
            np.save(latent_variables_out_path + str(num_count) + ".npy", zs_out)
            if args.store_final_reconstruction_waveform:
                np.save(shots_out_path + str(num_count) + ".npy", pred_sum)
            np.save(losses_out_path + str(num_count) + ".npy", np.array(losses))
            num_count += 1
            model_try += 1
            break
    fwi_loss.reset()

    print(acc)
    if args.error_termination and not latent_diverged:
        if rel_error <= args.seismic_relative_error and (args.use_well and acc >= args.well_accuracy):
            logger.info(
                'COMPLETED Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + ' Loss: ' + str(rel_error))

            zs_out = np.array(zs)
            np.save(latent_variables_out_path + str(num_count) + ".npy", zs_out)
            if args.store_final_reconstruction_waveform:
                np.save(shots_out_path + str(num_count) + ".npy", pred_sum)
            np.save(losses_out_path + str(num_count) + ".npy", np.array(losses))
            num_count += 1
            model_try += 1

        elif rel_error <= args.seismic_relative_error and not args.use_well:
            logger.info(
                'COMPLETED Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + ' Loss: ' + str(rel_error))

            zs_out = np.array(zs)
            np.save(latent_variables_out_path + str(num_count) + ".npy", zs_out)
            if args.store_final_reconstruction_waveform:
                np.save(shots_out_path + str(num_count) + ".npy", pred_sum)
            np.save(losses_out_path + str(num_count) + ".npy", np.array(losses))
            num_count += 1
            model_try += 1

        else:
            logger.info(
                'NOT COMPLETED Optimization: ' + str(num_count) + ' Iteration: ' + str(i) + ' Loss: ' + str(rel_error))

