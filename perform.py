import argparse
import gym
from gym import wrappers
import logging
import numpy as np
import tensorflow as tf

import rl.policies

from train import preprocess_wrap
import train_spec

def get_args():
  parser = argparse.ArgumentParser()
  parser.add_argument(
      "--env-id",
      required=True
  )
  parser.add_argument(
      "--checkpoint",
      required=True
  )
  parser.add_argument(
      "--policy",
      required=True
  )
  parser.add_argument(
      "--policy-name",
  )
  parser.add_argument(
      "--num-episodes",
      type=int,
      default=1
  )
  parser.add_argument(
      "--render",
      action="store_const",
      dest="render",
      const=True,
      default=False
  )
  parser.add_argument(
      "--record",
      default=None
  )
  args = parser.parse_args()
  return args

def main():
  args = get_args()

  env = preprocess_wrap(gym.make(args.env_id))
  if args.record is not None:
    env = wrappers.Monitor(env, args.record,
                           video_callable=lambda episode_id: True)
  policy_class = getattr(rl.policies, args.policy + "Policy")
  policy = train_spec.create_policy(
      env, policy_class,
      name=args.policy_name or (args.policy + "Policy_global"))
  logging.info("Using {} policy".format(policy_class))
  policy.build()
  with tf.Session() as sess:
    saver = tf.train.Saver().restore(sess, args.checkpoint)
    rewards = np.zeros([args.num_episodes])
    mean_reward = 0
    for i in range(args.num_episodes):
      obs = env.reset()
      done = False
      while not done:
        if args.render:
          env.render()
        action = policy.act(obs, sess=sess)[0]
        obs, rew, done, _ = env.step(action)
        rewards[i] += rew
      mean_reward += 1 / (i + 1) * (rewards[i] - mean_reward)
      logging.info(
          "Episode #{} reward: {}; mean reward {}"\
            .format(i + 1, rewards[i], mean_reward))
    if args.num_episodes > 5:
      print("--\nMean reward: {}, std: {}"\
          .format(np.mean(rewards), np.std(rewards)))


if __name__ == "__main__":
  main()
