# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#		 http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""A binary to train CIFAR-10 using multiple GPU's with synchronous updates.

Accuracy:
CNN_S_multi_gpu_train.py achieves ~86% accuracy after 100K steps (256
epochs of data) as judged by CNN_S_eval.py.

Speed: With batch_size 128.

System				| Step Time (sec/batch)	|		 Accuracy
--------------------------------------------------------------------
1 Tesla K20m	| 0.35-0.60							| ~86% at 60K steps	(5 hours)
1 Tesla K40m	| 0.25-0.35							| ~86% at 100K steps (4 hours)
2 Tesla K20m	| 0.13-0.20							| ~84% at 30K steps	(2.5 hours)
3 Tesla K20m	| 0.13-0.18							| ~84% at 30K steps
4 Tesla K20m	| ~0.10									| ~84% at 30K steps

Usage:
Please see the tutorial and website for how to download the CIFAR-10
data set, compile the program and train the model.

http://tensorflow.org/tutorials/deep_cnn/
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os.path
import re
import time

import numpy as np
from six.moves import xrange	# pylint: disable=redefined-builtin
import tensorflow as tf
slim = tf.contrib.slim

import CNN_S
from imagenet_data import *

FLAGS = tf.app.flags.FLAGS
import flags


def tower_loss( scope, dataset ):
	"""Calculate the total loss on a single tower running the CIFAR model.

	Args:
		scope: unique prefix string identifying the CIFAR tower, e.g. 'tower_0'

	Returns:
		 Tensor of shape [] containing the total loss for a batch of data
	"""
	# Get images and labels for CIFAR-10.
	images, labels = distorted_inputs( dataset )

	# Build inference Graph.
	#logits, end_points = CNN_S.inference_5x5_conv345(
	logits, end_points = CNN_S.inference_woBN(
	#logits, end_points = CNN_S.inference(
	#logits, end_points = CNN_S.inference_5x5_conv345_withPaddedPooling(
							images, dataset.num_classes(), phase_train= tf.constant(True))

	# Calculate predictions
	top_5_op = tf.nn.in_top_k(logits, labels-1, 5)

	# Build the portion of the Graph calculating the losses. Note that we will
	# assemble the total_loss using a custom function below.
	_ = CNN_S.loss(logits, labels)

	# Assemble all of the losses for the current tower only.
	losses = tf.get_collection('losses', scope)

	# Calculate the total loss for the current tower.
	total_loss = tf.add_n(losses, name='total_loss')

	# Attach a scalar summary to all individual losses and the total loss; do the
	# same for the averaged version of the losses.
	for l in losses + [total_loss]:
		# Remove 'tower_[0-9]/' from the name in case this is a multi-GPU training
		# session. This helps the clarity of presentation on tensorboard.
		loss_name = re.sub('%s_[0-9]*/' % CNN_S.TOWER_NAME, '', l.op.name)
		tf.summary.scalar(loss_name, l)

	return total_loss, top_5_op

def average_gradients(tower_grads):
	"""Calculate the average gradient for each shared variable across all towers.

	Note that this function provides a synchronization point across all towers.

	Args:
		tower_grads: List of lists of (gradient, variable) tuples. The outer list
			is over individual gradients. The inner list is over the gradient
			calculation for each tower.
	Returns:
		 List of pairs of (gradient, variable) where the gradient has been averaged
		 across all towers.
	"""
	average_grads = []
	for grad_and_vars in zip(*tower_grads):
		# Note that each grad_and_vars looks like the following:
		#	 ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
		grads = []
		for g, _ in grad_and_vars:
			# Add 0 dimension to the gradients to represent the tower.
			expanded_g = tf.expand_dims(g, 0)

			# Append on a 'tower' dimension which we will average over below.
			grads.append(expanded_g)

		# Average over the 'tower' dimension.
		grad = tf.concat(0, grads)
		grad = tf.reduce_mean(grad, 0)

		# Keep in mind that the Variables are redundant because they are shared
		# across towers. So .. we will just return the first tower's pointer to
		# the Variable.
		v = grad_and_vars[0][1]
		grad_and_var = (grad, v)
		average_grads.append(grad_and_var)
	return average_grads

def average_accuracy(tower_top_k_ops):
	"""Calculate the average gradient for each shared variable across all towers.

	Note that this function provides a synchronization point across all towers.

	Args:
		tower_grads: List of lists of (gradient, variable) tuples. The outer list
			is over individual gradients. The inner list is over the gradient
			calculation for each tower.
	Returns:
		 List of pairs of (gradient, variable) where the gradient has been averaged
		 across all towers.
	"""
	average_top_k = None
	for top_k_op in tower_top_k_ops:
		# Average over the 'tower' dimension.
		top_k = tf.to_float(top_k_op)
		top_k = tf.concat(0,top_k)
		top_k = tf.reduce_mean(top_k,0)

		if average_top_k is None :
			average_top_k = top_k
		else:
			average_top_k = average_top_k + top_k
	return average_top_k/len(tower_top_k_ops)


def train( dataset ):
	"""Train CIFAR-10 for a number of steps."""
	with tf.Graph().as_default(), tf.device('/cpu:0'):
		# Create a variable to count the number of train() calls. This equals the
		# number of batches processed * FLAGS.num_gpus.
		global_step = tf.get_variable('global_step', [],
				initializer=tf.constant_initializer(FLAGS.starting_step), trainable=False)

		# Calculate the learning rate schedule.
		num_batches_per_epoch = (dataset.num_examples_per_epoch() / FLAGS.batch_size)
		decay_steps = int(num_batches_per_epoch * CNN_S.NUM_EPOCHS_PER_DECAY)
		print( num_batches_per_epoch )
		print( decay_steps )

		# Decay the learning rate exponentially based on the number of steps.
		lr = tf.train.exponential_decay(CNN_S.INITIAL_LEARNING_RATE,
												global_step,
												decay_steps,
												CNN_S.LEARNING_RATE_DECAY_FACTOR,
												staircase=True)

		# Create an optimizer that performs gradient descent.
		opt = tf.train.GradientDescentOptimizer(lr)

		# Start running operations on the Graph. allow_soft_placement must be set to
		# True to build towers on GPU, as some of the ops do not have GPU
		# implementations.
		sess = tf.Session(config=tf.ConfigProto(
				allow_soft_placement=True,
				log_device_placement=FLAGS.log_device_placement))

		# Calculate the gradients for each model tower.
		tower_grads = []
		tower_top_5_ops = []
		for i in xrange(FLAGS.num_gpus):
			with tf.device('/gpu:%d' % i):
				with tf.name_scope('%s_%d' % (CNN_S.TOWER_NAME, i)) as scope:
					# Calculate the loss for one tower of the CIFAR model. This function
					# constructs the entire CIFAR model but shares the variables across
					# all towers.
					loss, top_5_op = tower_loss(scope, dataset)
					#print( sess.run( toPrint_labels[0] ) )

					# Reuse variables for the next tower.
					tf.get_variable_scope().reuse_variables()

					# Retain the summaries from the final tower.
					summaries = tf.get_collection(tf.GraphKeys.SUMMARIES, scope)

					# Calculate the gradients for the batch of data on this CIFAR tower.
					grads = opt.compute_gradients(loss)

					# Keep track of the gradients across all towers.
					tower_grads.append(grads)

					# Keep track of the top 5 ops across all towers.
					tower_top_5_ops.append(top_5_op)

		# We must calculate the mean of each gradient. Note that this is the
		# synchronization point across all towers.
		grads = average_gradients(tower_grads)

		#accuracy = average_accuracy( tower_top_5_ops )

		# Add a summary to track the learning rate.
		summaries.append(tf.summary.scalar('learning_rate', lr))

		# Add histograms for gradients.
		for grad, var in grads:
			if grad is not None:
				summaries.append(
						tf.summary.histogram(var.op.name + '/gradients',
												grad))

		# Apply the gradients to adjust the shared variables.
		apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)

		# Add histograms for trainable variables.
		for var in tf.trainable_variables():
			summaries.append(
					tf.summary.histogram(var.op.name, var))

		# Track the moving averages of all trainable variables.
		variable_averages = tf.train.ExponentialMovingAverage(
				CNN_S.MOVING_AVERAGE_DECAY, global_step)
		variables_averages_op = variable_averages.apply(tf.trainable_variables())

		# Group all updates to into a single train op.
		train_op = tf.group(apply_gradient_op, variables_averages_op)

		# Create a saver.
		saver = tf.train.Saver(tf.global_variables())

		# Build the summary operation from the last tower summaries.
		summary_op = tf.summary.merge(summaries)

		# Build an initialization operation to run below.
		init = tf.global_variables_initializer()

		sess.run(init)

		if FLAGS.pretrained_model_checkpoint_path:
			#assert tf.gfile.Exists(FLAGS.pretrained_model_checkpoint_path)
			variables_to_restore = slim.get_variables(scope="CNN_S")
			restorer = tf.train.Saver(variables_to_restore)
			#saver = tf.train.import_meta_graph('/tmp/model.ckpt.meta')
			restorer.restore(sess, FLAGS.pretrained_model_checkpoint_path)
			print('%s: Pre-trained model restored from %s' %(datetime.now(), FLAGS.pretrained_model_checkpoint_path))

		# Start the queue runners.
		tf.train.start_queue_runners(sess=sess)

		summary_writer = tf.summary.FileWriter(FLAGS.train_dir, sess.graph)

		
		for step in xrange(FLAGS.starting_step,FLAGS.max_steps):
			start_time = time.time()
			_, loss_val, top_5_pred = sess.run([train_op, loss, top_5_op])
			top_5_acc = np.sum(top_5_pred)*1.0/FLAGS.batch_size
			duration = time.time() - start_time

			assert not np.isnan(loss_val), 'Model diverged with loss = NaN'

			if step % 10 == 0:
				num_examples_per_step = FLAGS.batch_size * FLAGS.num_gpus
				examples_per_sec = num_examples_per_step / duration
				sec_per_batch = duration / FLAGS.num_gpus

				format_str = ('%s step %d, loss = %.2f, top5 = %.2f (%.1f ex/sec; %.3f '
											'sec/batch)')
				print (format_str % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), step, loss_val, top_5_acc,
														 examples_per_sec, sec_per_batch))

			if step % 100 == 0:
				summary_str = sess.run(summary_op)
				summary_writer.add_summary(summary_str, step)

			# Save the model checkpoint periodically.
			if step % 1000 == 0 or (step + 1) == FLAGS.max_steps:
				checkpoint_path = os.path.join(FLAGS.train_dir, 'model.ckpt')
				saver.save(sess, checkpoint_path, global_step=step)


def main(argv=None):	# pylint: disable=unused-argument
	dataset = ImageNetData( subset=FLAGS.subset )
	train( dataset )


if __name__ == '__main__':
	tf.app.run()
