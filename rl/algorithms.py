from contextlib import contextmanager
import logging
import numpy as np
import os
import queue
import tensorflow as tf

import rl.policies
from rl.trajectory import gae, TrajectoryProducer

USE_DEFAULT = object()


class BaseA3CAlgorithm(object):

  def __init__(self,
               env, *,
               trajectory_length,
               global_policy,
               local_policy=None,
               queue=queue.Queue(maxsize=5),
               entropy_coef=0.01,
               value_loss_coef=0.25,
               name=None):
    if local_policy is not None:
      self.sync_ops = tf.group(*[
          v1.assign(v2)
          for v1, v2 in zip(local_policy.var_list(), global_policy.var_list())
        ])
    else:
      self.sync_ops = None
    self.global_policy = global_policy
    self.local_policy = local_policy or global_policy
    self.trajectory_producer = TrajectoryProducer(
        env, self.local_policy, trajectory_length, queue)
    if name is None:
      name = self.__class__.__name__
    with tf.variable_scope(None, name) as scope:
      self.scope = scope
      self.global_step = tf.train.create_global_step()
      self.actions = tf.placeholder(self.global_policy.distribution.dtype,
                                    [None], name="actions")
      self.advantages = tf.placeholder(tf.float32, [None], name="advantages")
      self.value_targets = tf.placeholder(tf.float32, [None],
                                          name="value_targets")
      with tf.name_scope("loss"):
        pd = self.local_policy.distribution
        with tf.name_scope("policy_loss"):
          self.policy_loss = tf.reduce_sum(
              pd.neglogp(self.actions) * self.advantages)
          self.policy_loss -= entropy_coef * tf.reduce_sum(pd.entropy())
        with tf.name_scope("value_loss"):
          self.v_loss = tf.reduce_sum(tf.square(
              tf.squeeze(self.local_policy.value_preds) - self.value_targets))
        self.loss = self.policy_loss + value_loss_coef * self.v_loss
        self.gradients = tf.gradients(self.loss, self.local_policy.var_list())
        self.grads_and_vars = zip(
            self.global_policy.preprocess_gradients(self.gradients),
            self.global_policy.var_list()
          )
      self._init_summaries()

  def _init_summaries(self):
    with tf.variable_scope("summaries") as scope:
      tf.summary.scalar("value_preds",
                        tf.reduce_mean(self.local_policy.value_preds))
      tf.summary.scalar("value_targets",
                        tf.reduce_mean(self.value_targets))
      tf.summary.scalar(
          "distribution_entropy",
          tf.reduce_mean(self.local_policy.distribution.entropy()))
      batch_size = tf.to_float(tf.shape(self.actions)[0])
      tf.summary.scalar("policy_loss", self.policy_loss / batch_size)
      tf.summary.scalar("value_loss", self.v_loss / batch_size)
      tf.summary.scalar("loss", self.loss / batch_size)
      tf.summary.scalar("gradient_norm", tf.global_norm(self.gradients))
      tf.summary.scalar(
          "policy_norm", tf.global_norm(self.local_policy.var_list()))
      summaries = tf.get_collection(tf.GraphKeys.SUMMARIES, scope=scope.name)
      self.summaries = tf.summary.merge(summaries)

  def _get_train_op(self, optimizer):
    with tf.name_scope("train_op"):
      batch_size = tf.to_int64(tf.shape(self.local_policy.inputs)[0])
      return tf.group(
          optimizer.apply_gradients(self.grads_and_vars),
          self.global_step.assign_add(batch_size)
        )

  def _get_default_config(self):
    return tf.ConfigProto(intra_op_parallelism_threads=1,
                          inter_op_parallelism_threads=2)

  def _get_feed_dict(self, sess, gamma, lambda_):
    trajectory = self.trajectory_producer.next()
    advantages, value_targets = gae(
        self.local_policy, trajectory, gamma=gamma, lambda_=lambda_, sess=sess)
    feed_dict = {
        self.local_policy.inputs:
            trajectory.observations[:trajectory.num_timesteps],
        self.actions: trajectory.actions[:trajectory.num_timesteps],
        self.advantages: advantages,
        self.value_targets: value_targets
    }
    if self.local_policy.get_state() is not None:
      feed_dict[self.local_policy.state_in] = trajectory.policy_states[0]
    return feed_dict

  @contextmanager
  def _managed_session(self, checkpoint_dir, checkpoint=None,
                       checkpoint_period=None, hooks=None, config=USE_DEFAULT):
    if config == USE_DEFAULT:
      config = self._get_default_config()
    hooks = hooks or []
    saver = tf.train.Saver()
    if checkpoint_period is not None:
      hooks += [
          tf.train.CheckpointSaverHook(
            checkpoint_dir=checkpoint_dir, saver=saver,
            save_steps=checkpoint_period)
        ]
    elif not any(isinstance(h, tf.train.CheckpointSaverHook) for h in hooks):
      logging.warning(
        "Starting training without checkpoint saver hook."\
        " No checkpoint period provided and no explicit saver hook specified.")
    with tf.train.SingularMonitoredSession(hooks, config=config) as sess:
      if checkpoint is not None:
        saver.restore(sess, checkpoint)
      yield sess

  def train(self,
            optimizer,
            num_steps,
            logdir,
            summary_period,
            checkpoint_period=None,
            hooks=None,
            gamma=0.99,
            lambda_=0.95,
            checkpoint=None,
            config=USE_DEFAULT):
    train_op = self._get_train_op(optimizer)
    summary_writer = tf.summary.FileWriterCache.get(logdir)
    with self._managed_session(logdir, checkpoint, checkpoint_period,
                               hooks, config=config) as sess:
      summary_writer.add_graph(sess.graph)
      step = sess.run(self.global_step)
      # Always add summary on first step.
      last_summary_step = step - summary_period
      logging.info("Beginning training from step {}".format(step))
      self.trajectory_producer.start(summary_writer, summary_period, sess=sess)
      while not sess.should_stop() and step < num_steps:
        if self.sync_ops is not None:
          sess.run(self.sync_ops)
        feed_dict = self._get_feed_dict(sess, gamma, lambda_)
        if step - last_summary_step >= summary_period:
          policy_loss, v_loss, summary = sess.run(
            [self.policy_loss, self.v_loss, self.summaries, train_op],
            feed_dict)[:-1]
          logging.info("Step #{} Policy loss: {:.4f}, Value Loss: {:.4f}"\
                       .format(step, policy_loss, v_loss))
          summary_writer.add_summary(summary, step)
          last_summary_step = step
        else:
          sess.run(train_op, feed_dict)
        step = sess.run(self.global_step)
