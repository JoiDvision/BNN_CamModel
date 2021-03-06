import os
import datetime
import numpy as np
import tensorflow as tf
from tqdm import trange
from utils.misc import write_log
from utils.visualization import plot_weight_posteriors, plot_held_out
from model_lib import VanillaCNN
keras = tf.keras


class BaseTrainer(object):
    def __init__(self, params):
        self.params = params
        self.constrained_weights = None
        self.brand_models = self.params.dataloader.brand_models
        self.num_cls = len(self.brand_models)
        self.log_file = self.params.log.log_file
        self.train_loss = keras.metrics.Mean(
                        name='self.train_loss')
        self.train_acc = keras.metrics.CategoricalAccuracy(
                        name='self.train_accuracy')
        self.eval_loss = keras.metrics.Mean(
                    name='eval_loss')
        self.eval_acc = keras.metrics.CategoricalAccuracy(
                    name='eval_accuracy')
        self.optimizer = keras.optimizers.Adam(
                            learning_rate=self.params.trainer.lr)
        self.loss_object = keras.losses.CategoricalCrossentropy(from_logits=True)

    # focal loss produce more guaranteed a quicker converge for training
    def focal_loss(self, labels, logits, gamma=2.0, alpha=4.0):
        """
        focal loss for multi-classification
        FL(p_t)=-alpha(1-p_t)^{gamma}ln(p_t)
        gradient is d(Fl)/d(p_t) not d(Fl)/d(x) as described in paper
        d(Fl)/d(p_t) * [p_t(1-p_t)] = d(Fl)/d(x)
        Lin, T.-Y., Goyal, P., Girshick, R., He, K., & Dollár, P. (2017).
        https://doi.org/10.1016/j.ajodo.2005.02.022
        code from:https://github.com/zhezh/focalloss/blob/master/focalloss.py
        Args:
            labels: ground truth one-hot labels, shape of [batch_size].
            logits: model's output, shape of [batch_size, num_cls].
            gamma: hyper-parameter.
            alpha: hyper-parameter.
        Return: 
            reduced_fl: focal loss, shape of [batch_size]
        """
        epsilon = 1.e-9
        softmax = tf.nn.softmax(logits)
        num_cls = softmax.shape[1]
        model_out = tf.math.add(softmax, epsilon)
        ce = tf.math.multiply(labels, -tf.math.log(model_out))
        weight = tf.math.pow(tf.math.subtract(1., model_out), gamma)
        fl = tf.math.multiply(alpha, tf.math.multiply(weight, ce))
        reduced_fl = tf.math.reduce_sum(fl, axis=1)
        return reduced_fl

    def compute_steps(self, dataset, batch_size):
        """
        compute number of steps for train/val loops.
        Args:
            dataset: type of dataset, train/val/test.
            batch_size: batch size.
        Return:
            num_steps: number of step needed to iterate the whole dataset.
        """
        size = 0
        for m in self.brand_models:
            size += len(os.listdir(os.path.join(
                                    self.params.dataloader.patch_dir, 
                                    dataset, m)))
        num_steps = ((size + batch_size - 1) // batch_size)
        return num_steps

    def tensorboard_init(self):
        """
        initilization for tensorboard.
        """
        # variables for early stopping and saving best models   
        # random guessing
        self.best_acc = 1.0 / self.num_cls
        self.best_loss = 10000
        self.step_idx = 0
        current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        train_log_dir = os.path.join(self.params.log.tensorboard_dir,
                self.params.run.name,
                current_time, 'train')
        val_log_dir = os.path.join(self.params.log.tensorboard_dir,
                self.params.run.name,
                current_time, 'val')
        self.train_writer = tf.summary.create_file_writer(train_log_dir)
        self.val_writer = tf.summary.create_file_writer(val_log_dir)
        with self.val_writer.as_default():
            tf.summary.scalar('loss', self.best_loss, step=self.step_idx)
            tf.summary.scalar('accuracy', self.best_acc, step=self.step_idx)
            self.val_writer.flush()

    def checkpoint_init(self):
        """
        if checkpoint exists, restore the checkpoint. if not, initialize for saving checkpoints. 
        """
        # save model to a checkpoint
        self.ckpt = tf.train.Checkpoint(step=tf.Variable(1),
                        optimizer=self.optimizer,
                        net=self.model)
        self.manager = tf.train.CheckpointManager(self.ckpt, 
                        self.params.trainer.ckpt_dir,
                        max_to_keep=3)
        status = self.ckpt.restore(
                    self.manager.latest_checkpoint)
        if self.manager.latest_checkpoint:
            # status.assert_existing_objects_matched()
            msg = ("\nRestored from {}\n".format(self.manager.latest_checkpoint))
        else:
            msg = ("\nInitializing from scratch.\n")
        write_log(self.log_file, msg)


class VanillaTrainer(BaseTrainer):
    """
    training loop for vanilla (baseline) CNN.
    """
    def __init__(self, params, model):
        super(VanillaTrainer, self).__init__(params)
        self.model = model
        self.num_train_steps = self.compute_steps('train',
                                        self.params.trainer.batch_size)
        self.num_val_steps = self.compute_steps('val',
                                self.params.trainer.batch_size)
        self.num_test_steps = self.compute_steps('test',
                                self.params.evaluate.batch_size)

    @tf.function
    def train_step(self, images, labels):
        with tf.GradientTape() as tape:
            logits = self.model(images, training=True)
            loss = self.loss_object(labels, logits)
        gradients = tape.gradient(loss, self.model.trainable_weights)
        self.optimizer.apply_gradients(zip(gradients,
                self.model.trainable_weights))
        self.train_loss.update_state(loss)
        self.train_acc.update_state(labels, logits)
        if self.step_idx % 150 == 0:
            with self.train_writer.as_default():
                tf.summary.histogram(
                    'constrained_conv_grad', 
                    gradients[0],
                    self.step_idx)
                tf.summary.histogram(
                    'constrained_conv_weights',
                    self.model.constrained_conv_layer.weights[0],
                    self.step_idx)
            self.train_writer.flush()

    @tf.function
    def eval_step(self, images, labels):
        with tf.GradientTape() as tape:
            logits = self.model(images)
            loss = self.loss_object(labels, logits)
        # number of samples for each class
        total = tf.math.reduce_sum(labels, axis=0)
        gt = tf.math.argmax(labels, axis=1)
        pred = tf.math.argmax(logits, axis=1)
        corr = labels[pred == gt]
        corr_count = tf.math.reduce_sum(corr, axis=0)
        self.eval_loss.update_state(loss)
        self.eval_acc.update_state(labels, logits)
        return corr_count, total

    def train(self, train_iter, val_iter):
        """
        train and validation.
        """
        self.model.build(input_shape=(None, 256, 256, 1))
        self.model.summary()
        self.tensorboard_init()
        self.checkpoint_init()
        stop_count = 0

        msg = ('... Training convolutional neural network\n\n')
        write_log(self.log_file, msg)
        for epoch in range(self.params.trainer.epochs):
            offset = epoch * self.num_train_steps
            self.eval_loss.reset_states()
            self.eval_acc.reset_states()
            self.train_loss.reset_states()
            self.train_acc.reset_states()

            # training loop
            for step in trange(self.num_train_steps):
                self.step_idx = offset + step
                images, labels = train_iter.get_next()
                self.train_step(images, labels)
                # self.constrained_conv_update()
                self.train_writer.flush()

                with self.train_writer.as_default():
                    tf.summary.scalar('loss', self.train_loss.result(), step=self.step_idx)
                    tf.summary.scalar('accuracy', self.train_acc.result(), step=self.step_idx)
                    self.train_writer.flush()

                if (step+1) % self.params.log.log_step == 0:
                    msg = (('Epoch: {}, Step: {}, '
                            'train loss: {:.3f}, train accuracy: {:.3%}\n')
                            .format(epoch, self.step_idx, 
                            self.train_loss.result(), 
                            self.train_acc.result()))
                    write_log(self.log_file, msg)

            # validation
            corr_ls = [0 for x in self.brand_models]
            total_ls = [0 for x in self.brand_models]
            for step in trange(self.num_val_steps):
                images, labels = val_iter.get_next()
                c, t = self.eval_step(images, labels)
                corr_ls = [sum(x) for x in zip(corr_ls, c)]
                total_ls = [sum(x) for x in zip(total_ls, t)]

            with self.val_writer.as_default():
                tf.summary.scalar('loss', self.eval_loss.result(), step=self.step_idx)
                tf.summary.scalar('accuracy', self.eval_acc.result(), step=self.step_idx)
                self.val_writer.flush()

            msg = 'val loss: {:.3f}, validation accuracy: {:.3%}\n'.format(
                    self.eval_loss.result(), self.eval_acc.result())
            write_log(self.log_file, msg)

            # print validation results
            for m, c, t in zip(self.brand_models, corr_ls, total_ls):
                acc = c / t
                msg = '{} accuracy: {:.3%}\n'.format(m, acc)
                write_log(self.log_file, msg)
            write_log(self.log_file, '\n')

            # save model with early stopping
            self.ckpt.step.assign_add(1)
            if self.eval_loss.result() < self.best_loss:
                self.best_acc = self.eval_acc.result()
                self.best_loss = self.eval_loss.result()
                stop_count = 0
                save_path = self.manager.save()
                msg = "Saved checkpoint for epoch {}: {}\n\n".format(epoch, self.params.trainer.ckpt_dir)
                write_log(self.log_file, msg)
            else:
                stop_count += 1
                if stop_count >= self.params.trainer.patience:
                    break
        msg = '\n... Finished training\n'
        write_log(self.log_file, msg)

    def evaluate(self, test_iter):
        """
        evalution.
        """
        self.model.build(input_shape=(None, 256, 256, 1))
        self.checkpoint_init()
        self.eval_acc.reset_states()
        self.eval_loss.reset_states()

        corr_ls = [0 for x in self.brand_models]
        total_ls = [0 for x in self.brand_models]
        # print(self.model.constrained_conv_layer.get_weights())
        for step in trange(self.num_test_steps):
            images, labels = test_iter.get_next()
            c, t = self.eval_step(images, labels)
            corr_ls = [sum(x) for x in zip(corr_ls, c)]
            total_ls = [sum(x) for x in zip(total_ls, t)]
        msg ='\n\ntest loss: {:.3f}, test accuracy: {:.3%}\n'.format(self.eval_loss.result(),
                                                            self.eval_acc.result())
        write_log(self.log_file, msg)
        for m, c, t in zip(self.brand_models, corr_ls, total_ls):
            msg = '{} accuracy: {:.3%}\n'.format(m, c / t)
            write_log(self.log_file, msg)

class EnsembleTrainer(object):
    def __init__(self, params, model):
        self.params = params
        self.model = model
        self.ckpt_prefix = self.params.trainer.ckpt_dir

    def train(self, train_iter, val_iter):
        """
        reuse the VanillaCNN class for training ensemble.
        """
        for i in range(self.params.trainer.num_ensemble):
            ensemble_idx = i
            self.params.trainer.ckpt_dir = os.path.join(
                            self.ckpt_prefix,
                            str(ensemble_idx))
            print(self.params.trainer.ckpt_dir)
            trainer = VanillaTrainer(self.params, self.model)
            trainer.train(train_iter, val_iter)
            # reset model weight for the next training
            self.model = VanillaCNN(self.params)

    def evaluate(self, test_iter):
        for i in range(self.params.trainer.num_ensemble):
            ensemble_idx = i
            self.params.trainer.ckpt_dir = os.path.join(
                            self.ckpt_prefix,
                            str(ensemble_idx))
            print(self.params.trainer.ckpt_dir)
            trainer = VanillaTrainer(self.params, self.model)
            trainer.evaluate(test_iter)

class BayesianTrainer(BaseTrainer):
    def __init__(self, params, model):
        super(BayesianTrainer, self).__init__(params)
        self.model = model
        self.num_train_steps = self.compute_steps('train',
                                        self.params.trainer.batch_size)
        self.num_val_steps = self.compute_steps('val',
                                self.params.trainer.batch_size)
        self.num_test_steps = self.compute_steps('test',
                                self.params.evaluate.batch_size)
        # use focal loss as loss objective for bnn.
        self.loss_object = self.focal_loss
        # lr_schedule = keras.optimizers.schedules.ExponentialDecay(
        #     self.params.trainer.lr,
        #     decay_steps=self.num_train_steps,
        #     decay_rate=self.params.trainer.decay_rate,
        #     staircase=True)
        self.optimizer = keras.optimizers.Adam(learning_rate=self.params.trainer.lr)
        self.kl_loss = keras.metrics.Mean(name='kl_loss')
        self.nll_loss = keras.metrics.Mean(name='nll_loss')
    
    def plot_weights(self, prior_fname, posterior_fname):
        """
        plot the weights distribution for each layer.
        Args:
            prior_fname: file path of the prior distribution plot.
            posterior_fname: file path of the posterior plot.
        """
        # only plot the flipout layers, weights of other layers are deterministic after training.
        names = [layer.name for layer in self.model.layers
                    if 'flipout' in layer.name]
        # prior distribution of kernel
        pm_vals = [layer.kernel_prior.mean() 
                    for layer in self.model.layers
                    if 'flipout' in layer.name]
        ps_vals = [layer.kernel_prior.stddev()
                    for layer in self.model.layers
                    if 'flipout' in layer.name] 
        # posterior distribution of kernel
        qm_vals = [layer.kernel_posterior.mean() 
                for layer in self.model.layers
                if 'flipout' in layer.name]
        qs_vals = [layer.kernel_posterior.stddev() 
                for layer in self.model.layers
                if 'flipout' in layer.name]
        plot_weight_posteriors(names, pm_vals, ps_vals, 
                                fname=prior_fname)
        plot_weight_posteriors(names, qm_vals, qs_vals, 
                                fname=posterior_fname)

    @tf.function
    def train_step(self, images, labels):
        with tf.GradientTape() as tape:
            logits = self.model(images, training=True)
            nll = self.loss_object(labels, logits)
            kl = sum(self.model.losses)
            loss = nll + kl
        gradients = tape.gradient(loss, self.model.trainable_weights)
        self.optimizer.apply_gradients(zip(gradients, 
                self.model.trainable_weights))
        self.kl_loss.update_state(kl)  
        self.nll_loss.update_state(nll)
        self.train_loss.update_state(loss)
        self.train_acc.update_state(labels, logits)
        # show histogram of gradients in tensorboard
        # if self.step_idx % 150 == 0:
        #     with self.train_writer.as_default():
        #         tf.summary.histogram(
        #             'constrained_conv_grad', 
        #             gradients[0],
        #             self.step_idx)
        #         tf.summary.histogram(
        #             'constrained_conv_weights',
        #             self.model.constrained_conv_layer.weights[0],
        #             self.step_idx)
        #     self.train_writer.flush()

    @tf.function
    def eval_step(self, images, labels):
        with tf.GradientTape() as tape:
            logits = self.model(images)
            nll = self.loss_object(labels, logits)
            kl = sum(self.model.losses)
            loss = nll + kl
        # number of samples for each class
        total = tf.math.reduce_sum(labels, axis=0)
        gt = tf.math.argmax(labels, axis=1)
        pred = tf.math.argmax(logits, axis=1)
        corr = labels[pred == gt]
        corr_count = tf.math.reduce_sum(corr, axis=0)
        self.eval_loss.update_state(loss)
        self.eval_acc.update_state(labels, logits)
        return corr_count, total

    def train(self, train_iter, val_iter):
        self.model.build(input_shape=(None, 256, 256, 1))
        self.model.summary()
        self.tensorboard_init()
        self.checkpoint_init()

        msg = ('... Training bayesian convolutional neural network\n\n')
        write_log(self.log_file, msg)
        for epoch in range(self.params.trainer.epochs):
            offset = epoch * self.num_train_steps
            self.eval_loss.reset_states()
            self.eval_acc.reset_states()
            self.train_loss.reset_states()
            self.train_acc.reset_states()
            self.kl_loss.reset_states()
            self.nll_loss.reset_states()

            # train
            for step in trange(self.num_train_steps):
                self.step_idx = offset + step
                images, labels = train_iter.get_next()
                self.train_step(images, labels)
                # self.constrained_conv_update()
                self.train_writer.flush()

                with self.train_writer.as_default():
                    tf.summary.scalar('loss', self.train_loss.result(), step=self.step_idx)
                    tf.summary.scalar('accuracy', self.train_acc.result(), step=self.step_idx)
                    tf.summary.scalar('kl_loss', self.kl_loss.result(), step=self.step_idx)
                    tf.summary.scalar('nll_loss', self.nll_loss.result(), step=self.step_idx)
                    self.train_writer.flush()

                if (step+1) % self.params.log.log_step == 0:
                    msg = ('Epoch: {}, Step: {}, '
                            'train loss: {:.3f}, train accuracy: {:.3%}, '
                            'kl loss: {:.3f}, nll loss: {:.3f}\n'
                            .format(epoch, self.step_idx + 1,
                                    self.train_loss.result(),
                                    self.train_acc.result(),
                                    self.kl_loss.result(),
                                    self.nll_loss.result()))
                    write_log(self.log_file, msg)

            # validation
            corr_ls = [0 for x in self.brand_models]
            total_ls = [0 for x in self.brand_models]
            for step in trange(self.num_val_steps):
                images, labels = val_iter.get_next()
                c, t = self.eval_step(images, labels)
                corr_ls = [sum(x) for x in zip(corr_ls, c)]
                total_ls = [sum(x) for x in zip(total_ls, t)]

            with self.val_writer.as_default():
                tf.summary.scalar('loss', self.eval_loss.result(), step=self.step_idx)
                tf.summary.scalar('accuracy', self.eval_acc.result(), step=self.step_idx)
                self.val_writer.flush()

            msg = 'val loss: {:.3f}, validation accuracy: {:.3%}\n'.format(
                    self.eval_loss.result(), self.eval_acc.result())
            write_log(self.log_file, msg)

            # print validation results
            for m, c, t in zip(self.brand_models, corr_ls, total_ls):
                msg = '{} accuracy: {:.3%}\n'.format(m, c / t)
                write_log(self.log_file, msg)
            write_log(self.log_file, '\n')

            self.ckpt.step.assign_add(1)
            if (self.best_acc - self.eval_acc.result()) <= 0.02 and \
                self.eval_loss.result() <= self.best_loss:
                self.best_acc = self.eval_acc.result()
                self.best_loss = self.eval_loss.result()
                stop_count = 0
                save_path = self.manager.save()
                msg = ("Saved checkpoint for epoch {}: {}\n\n"
                        .format(epoch, save_path))
                write_log(self.log_file, msg)
            else:
                stop_count += 1
                if stop_count >= self.params.trainer.patience:
                    break
        msg = '\n... Finished training\n'
        write_log(self.log_file, msg)

    def mc_out_stats(self, images, labels, model, num_monte_carlo, fname):
        """
        visulize Monte Carlo predictions for images.
        Args:
            images: input images.
            labels: corresponding one-hot labels for images.
            model: neural network. 
            num_monte_carlo: times of sampling from the network.
            fname: output file path.
        """
        mc_softmax_prob_out = []
        for i in range(num_monte_carlo):
            logits_out = model(images)
            softmax_out = tf.nn.softmax(logits_out)
            mc_softmax_prob_out.append(softmax_out)
        mc_softmax_prob_out = np.asarray(mc_softmax_prob_out)
        plot_held_out(images, labels, self.brand_models, mc_softmax_prob_out, fname)

    def evaluate(self, test_iter):
        self.model.build(input_shape=(None, 256, 256, 1))
        self.plot_weights(self.params.evaluate.initialized_prior,
                            self.params.evaluate.initialized_posterior)
        self.checkpoint_init()
        self.plot_weights(self.params.evaluate.trained_prior,
                    self.params.evaluate.trained_posterior)
        self.eval_acc.reset_states()
        self.eval_loss.reset_states()

        corr_ls = [0 for x in self.brand_models]
        total_ls = [0 for x in self.brand_models]
        # print(self.model.constrained_conv_layer.weights[0])
        for step in trange(self.num_test_steps):
            images, labels = test_iter.get_next()
            # visualize result examples every x step.
            # if step % 30 == 0:
            #     self.mc_out_stats(images, labels, self.model, num_monte_carlo=50, 
            #                     fname="results/image_uncertainty_{}.png".format(step))
            c, t = self.eval_step(images, labels)
            corr_ls = [sum(x) for x in zip(corr_ls, c)]
            total_ls = [sum(x) for x in zip(total_ls, t)]
        msg ='\n\ntest loss: {:.3f}, test accuracy: {:.3%}\n'.format(self.eval_loss.result(),
                                                            self.eval_acc.result())
        write_log(self.log_file, msg)
        for m, c, t in zip(self.brand_models, corr_ls, total_ls):
            msg = '{} accuracy: {:.3%}\n'.format(m, c / t)
            write_log(self.log_file, msg)
