import numpy as np
import tensorflow as tf
import params
import data_preparation as dp
import utils
import os
import model_lib
import seaborn as sns
from tqdm import trange
import matplotlib
matplotlib.use('Agg')
from matplotlib import figure
from matplotlib.backends import backend_agg
gpus = tf.config.experimental.list_physical_devices('GPU')
tf.config.experimental.set_memory_growth(gpus[0], True)
AUTOTUNE = tf.data.experimental.AUTOTUNE


def stats(ckpt_dir, stats_fig, fname):
    train_size = 0
    for m in params.brand_models:
        train_size += len(os.listdir(os.path.join(params.patch_dir, 'train', m)))
    model = model_lib.bnn(train_size)

    ckpt = tf.train.Checkpoint(
            step=tf.Variable(1), 
            optimizer=tf.keras.optimizers.Adam(lr=params.HParams['init_learning_rate']),
            net=model)
    manager = tf.train.CheckpointManager(ckpt, ckpt_dir, max_to_keep=3)
    ckpt.restore(manager.latest_checkpoint)

    ds, num_test_batches = dp.aligned_ds(params.patch_dir, 
                                      params.brand_models)
    unseen_ds, num_unseen_batches = dp.aligned_ds(params.unseen_dir, 
                                               params.unseen_brand_models,
                                               num_batches=num_test_batches)
    kaggle_models = os.listdir(os.path.join('data', 'kaggle'))
    kaggle_ds, num_kaggle_batches = ds.aligned_ds(params.kaggle_dir,
                                                   kaggle_models,
                                                   num_batches=num_test_batches)

    in_dataset = (tf.data.Dataset.from_tensor_slices(ds)
                    .repeat()
                    .map(dp.parse_image, num_parallel_calls=AUTOTUNE)
                    .batch(params.BATCH_SIZE)
                    .prefetch(buffer_size=AUTOTUNE))

    unseen_dataset = (tf.data.Dataset.from_tensor_slices(unseen_ds)
                        .repeat()
                        .map(dp.parse_image, num_parallel_calls=AUTOTUNE)
                        .batch(params.BATCH_SIZE)
                        .prefetch(buffer_size=AUTOTUNE))

    kaggle_dataset = (tf.data.Dataset.from_tensor_slice(kaggle_ds
                        .repeat()
                        .map(dp.parse_image, num_parallel_calls=AUTOTUNE)
                        .batch(params.BATCH_SIZE)
                        .prefetch(buffer_size=AUTOTUNE)))

    jpeg_ds = dp.post_processing(ds, 'jpeg', 70)
    jpeg_dataset = (tf.data.Dataset.from_tensor_slices(jpeg_ds)
                    .repeat()
                    .map(dp.parse_image,
                         num_parallel_calls=AUTOTUNE)
                    .batch(params.BATCH_SIZE)
                    .prefetch(buffer_size=AUTOTUNE))

    blur_ds = dp.post_processing(ds, 'blur', 1.1)
    blur_dataset = (tf.data.Dataset.from_tensor_slices(blur_ds)
                    .repeat()
                    .map(dp.parse_image,
                         num_parallel_calls=AUTOTUNE)
                    .batch(params.BATCH_SIZE)
                    .prefetch(buffer_size=AUTOTUNE))
    
    noise_ds = dp.post_processing(ds, 'noise', 2.0)
    noise_dataset = (tf.data.Dataset.from_tensor_slices(noise_ds)
                        .repeat()
                        .map(dp.parse_image,
                             num_parallel_calls=AUTOTUNE)
                        .batch(params.BATCH_SIZE)
                        .prefetch(buffer_size=AUTOTUNE))
    
    in_iter = iter(in_dataset)
    unseen_iter = iter(unseen_dataset)
    kaggle_iter = iter(kaggle_dataset)
    jpeg_iter = iter(jpeg_dataset)
    blur_iter = iter(blur_dataset)
    noise_iter = iter(noise_dataset)

    inverse = False
    print('number of unseen batches {}'.format(num_unseen_batches))
    print('number of test batches {}'.format(num_test_batches))

    num_monte_carlo = [5, 10, 20, 30, 40, 50]
    targets = []
    for mc in num_monte_carlo:
        print("... In-distribution MC Statistics")
        in_entropy, in_epistemic = utils.mc_in_stats(in_iter, model, 
                                                    num_test_batches, 
                                                    mc)
        unseen_in_entropy, unseen_in_epistemic = in_entropy, in_epistemic

        # unseen images
        print("... UNSEEN out-of-distribution MC Statistics")
        unseen_entropy, unseen_epistemic, unseen_cls_count = \
                        utils.mc_out_stats(unseen_iter, model, 
                                            num_unseen_batches,
                                            mc)
        utils.log_mc_in_out(in_entropy,
                            unseen_entropy,
                            in_epistemic,
                            unseen_epistemic,
                            unseen_cls_count,
                            mc,
                            'UNSEEN', fname)

        print("... Kaggle out-of-distribution MC Statistics")
        kaggle_entropy, kaggle_epistemic, kaggle_cls_count = \
                        utils.mc_out_stats(kaggle_iter, model,
                                            num_kaggle_batches,
                                            mc)
        utils.log_mc_in_out(in_entropy,
                            kaggle_entropy,
                            in_epistemic,
                            kaggle_epistemic,
                            kaggle_cls_count,
                            mc,
                            'Kaggle', fname)

        print("... JPEG Out-of-distribution MC Statistics")
        jpeg_entropy, jpeg_epistemic, jpeg_cls_count = \
                        utils.mc_out_stats(jpeg_iter, model, 
                                            num_test_batches,
                                            mc)
        utils.log_mc_in_out(in_entropy,
                            jpeg_entropy,
                            in_epistemic,
                            jpeg_epistemic,
                            jpeg_cls_count,
                            mc,
                            'JPEG', fname)

        print("... BLUR Out-of-distribution MC Statistics")
        blur_entropy, blur_epistemic, blur_cls_count = \
                        utils.mc_out_stats(blur_iter, model, 
                                            num_test_batches,
                                            mc)
        utils.log_mc_in_out(in_entropy,
                            blur_entropy,
                            in_epistemic,
                            blur_epistemic,
                            blur_cls_count,
                            mc,
                            'BLUR', fname)

        print("... NOISE Out-of-distribution MC Statistics")
        noise_entropy, noise_epistemic, noise_cls_count = \
                        utils.mc_out_stats(noise_iter, model,
                                            num_test_batches,
                                            mc)
        utils.log_mc_in_out(in_entropy,
                            noise_entropy,
                            in_epistemic,
                            noise_epistemic,
                            noise_cls_count,
                            mc,
                            'NOISE', fname)

        labels = ['In Distribution', 'Unseen Models', 
                    'Kaggle', 'JPEG', 'Blurred', 'Noisy']

        targets.append([('entropy, unseen', 
                        [in_entropy, unseen_entropy]),
                        ('entropy, kaggle', 
                        [in_entropy, kaggle_entropy]),
                        ('entropy, jpeg', 
                        [in_entropy, jpeg_entropy]), 
                        ('entropy, blur models', 
                        [in_entropy, blur_entropy]),
                        ('entropy, noise', 
                        [in_entropy, noise_entropy]), 
                        ('epistemic, unseen', 
                        [in_epistemic, unseen_epistemic]),
                        ('epistemic, kaggle',
                        [in_epistemic, kaggle_epistemic]),
                        ('epistemic, jpeg', 
                        [in_epistemic, jpeg_epistemic]),
                        ('epistemic, blur models', 
                        [in_epistemic, blur_epistemic]),
                        ('epistemic, noise models', 
                        [in_epistemic, noise_epistemic])])
    
    # Plotting ROC and PR curves 
    fig = figure.Figure(figsize=(25, 10) 
                        if params.model_type == 'bnn' 
                        else (25, 5))
    canvas = backend_agg.FigureCanvasAgg(fig)
    fz = 15
    opt_list = []
    sns.set_style("darkgrid")
    for t, mc in zip(targets, num_monte_carlo):
        for i, (plotname, (safe, risky)) in enumerate(t):
            ax = fig.add_subplot(2, 5, i+1)

            fpr, tpr, opt, auroc = utils.roc_pr_curves(safe, risky, inverse)
            opt_list.append(opt)
            acc = np.sum((risky > opt[2]).astype(int)) / risky.shape[0]
            msg = (plotname + '\n'
                "false positive rate: {:.3%}, "
                "true positive rate: {:.3%}, "
                "threshold: {}, "
                "acc: {:.3%}\n".format(opt[0], opt[1], opt[2], acc))
            with open(fname, 'a') as f:
                f.write(msg)

            ax.plot(fpr, tpr, '-',
                    label='{} mc:{}'.format(mc, auroc),
                    lw=2)
            # ax.plot([0, 1], 'k-', lw=1, label='Base rate(0.5)')
            ax.legend(fontsize=10)
            ax.set_title(plotname, fontsize=fz)
            ax.set_xlabel("FPR", fontsize=fz)
            ax.set_ylabel("TPR", fontsize=fz)
            ax.grid(True)
    fig.suptitle('ROC curve of uncertainty binary detector (correct / in-distribution as positive)', y=1.07, fontsize=25)
    fig.tight_layout()
    canvas.print_figure(stats_fig, format='png')
    print('saved {}'.format(stats_fig))


if __name__ == "__main__":
    ckpt_dir = os.path.join('ckpts', params.database, params.model_type)
    stats_fig = os.path.join('results', params.database, params.model_type) + '_mc_stats.png'
    fname = os.path.join('results', params.database, params.model_type) + '_mc_stats.log'
    stats(ckpt_dir, stats_fig, fname)