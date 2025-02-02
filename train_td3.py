import os  
os.environ['CUDA_VISIBLE_DEVICES'] = "1"

import argparse
import datetime
import os
import pprint
import numpy as np
import torch

from tianshou.data import Collector, ReplayBuffer, VectorReplayBuffer
from tianshou.exploration import GaussianNoise
from tianshou.highlevel.logger import LoggerFactoryDefault
from tianshou.policy import TD3Policy
from tianshou.policy.base import BasePolicy
from tianshou.trainer import OffpolicyTrainer
from tianshou.utils.net.common import Net
from tianshou.utils.net.continuous import Actor, Critic
from tianshou.env import DummyVectorEnv

from train_env import TrainSinglePileEnv

def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=str, default="EV")
    parser.add_argument("--seed", type=int, default=20)
    parser.add_argument("--buffer-size" , type=int, default=50000)
    parser.add_argument("--actor-sizes" , type=int, nargs="*", default=[300,200])
    parser.add_argument("--critic-sizes", type=int, nargs="*", default=[400,300])
    parser.add_argument("--actor-lr"    , type=float, default=6e-4)
    parser.add_argument("--critic-lr"   , type=float, default=1e-3)
    parser.add_argument("--gamma"       , type=float, default=0.99)
    parser.add_argument("--tau"         , type=float, default=0.005)
    parser.add_argument("--exploration-noise", type=float, default=0.3)
    parser.add_argument("--policy-noise"     , type=float, default=0.05)
    parser.add_argument("--noise-clip"       , type=float, default=0.125)
    parser.add_argument("--update-actor-freq", type=int  , default=4)
    parser.add_argument("--start-timesteps"  , type=int  , default=200)
    parser.add_argument("--epoch"            , type=int  , default=15000)
    parser.add_argument("--step-per-epoch"   , type=int  , default=64)
    parser.add_argument("--step-per-collect" , type=int  , default=1)
    parser.add_argument("--update-per-step"  , type=int  , default=1)
    parser.add_argument("--n-step"       , type=int  , default=1)
    parser.add_argument("--batch-size"   , type=int  , default=64)
    parser.add_argument("--training-num" , type=int  , default=10)
    parser.add_argument("--test-num"     , type=int  , default=10)
    parser.add_argument("--logdir"       , type=str  , default="log")
    parser.add_argument("--render"       , type=float, default=0.0)
    parser.add_argument("--device"       , type=str  ,default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--resume-path"  , type=str  , default=None)
    parser.add_argument("--resume-id"    , type=str  , default=None)
    parser.add_argument(
        "--logger",
        type=str,
        default="tensorboard",
        choices=["tensorboard", "wandb"],
    )
    parser.add_argument("--wandb-project", type=str, default="mujoco.benchmark")
    parser.add_argument(
        "--watch",
        default=False,
        action="store_true",
        help="watch the play of pre-trained policy only",
    )
    return parser.parse_args()

def test_td3(args: argparse.Namespace = get_args()) -> None:
    env = TrainSinglePileEnv(N_piles = 10)
    train_envs = DummyVectorEnv([lambda: TrainSinglePileEnv(N_piles = 10) for _ in range(args.training_num)])
    test_envs  = DummyVectorEnv([lambda: TrainSinglePileEnv(N_piles = 10) for _ in range(args.training_num)])
    train_envs.seed(list(range(args.seed,args.seed+args.training_num)))
    test_envs.seed(list(range(args.seed+args.training_num,args.seed+args.training_num+args.test_num)))
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    args.max_action = env.action_space.high[0]
    args.exploration_noise = args.exploration_noise * args.max_action
    args.policy_noise = args.policy_noise * args.max_action
    args.noise_clip = args.noise_clip * args.max_action
    print("Observations shape:", args.state_shape)
    print("Actions shape:", args.action_shape)
    print("Action range:", np.min(env.action_space.low), np.max(env.action_space.high))
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    # model
    net_a = Net(state_shape=args.state_shape, hidden_sizes=args.actor_sizes, device=args.device)
    actor = Actor(net_a, args.action_shape, max_action=args.max_action, device=args.device).to(
        args.device,
    )
    actor_optim = torch.optim.Adam(actor.parameters(), lr=args.actor_lr)
    net_c1 = Net(
        state_shape=args.state_shape,
        action_shape=args.action_shape,
        hidden_sizes=args.critic_sizes,
        concat=True,
        device=args.device,
    )

    net_c2 = Net(
        state_shape=args.state_shape,
        action_shape=args.action_shape,
        hidden_sizes=args.critic_sizes,
        concat=True,
        device=args.device,
    )

    critic1 = Critic(net_c1, device=args.device).to(args.device)
    critic1_optim = torch.optim.Adam(critic1.parameters(), lr=args.critic_lr)
    critic2 = Critic(net_c2, device=args.device).to(args.device)
    critic2_optim = torch.optim.Adam(critic2.parameters(), lr=args.critic_lr)

    policy: TD3Policy = TD3Policy(
        actor=actor,
        actor_optim=actor_optim,
        critic=critic1,
        critic_optim=critic1_optim,
        critic2=critic2,
        critic2_optim=critic2_optim,
        tau=args.tau,
        gamma=args.gamma,
        exploration_noise=GaussianNoise(sigma=args.exploration_noise),
        policy_noise=args.policy_noise,
        update_actor_freq=args.update_actor_freq,
        noise_clip=args.noise_clip,
        estimation_step=args.n_step,
        action_space=env.action_space,
    )

    # load a previous policy
    if args.resume_path:
        policy.load_state_dict(torch.load(args.resume_path, map_location=args.device))
        print("Loaded agent from: ", args.resume_path)

    # collector
    buffer: VectorReplayBuffer | ReplayBuffer
    if args.training_num > 1:
        buffer = VectorReplayBuffer(args.buffer_size, len(train_envs))
    else:
        buffer = ReplayBuffer(args.buffer_size)
    train_collector = Collector(policy, train_envs, buffer, exploration_noise=True)
    test_collector  = Collector(policy, test_envs)
    train_collector.reset()
    train_collector.collect(n_step=args.start_timesteps, random=True)

    # log
    now = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    args.algo_name = "td3"
    # log_name = os.path.join(args.task, args.algo_name, str(args.seed), now)
    log_name = os.path.join(args.task, args.algo_name, str(3), now)
    log_path = os.path.join(args.logdir, log_name)
    

    # logger
    logger_factory = LoggerFactoryDefault()
    if args.logger == "wandb":
        logger_factory.logger_type = "wandb"
        logger_factory.wandb_project = args.wandb_project
    else:
        logger_factory.logger_type = "tensorboard"

    logger = logger_factory.create_logger(
        log_dir=log_path,
        experiment_name=log_name,
        run_id=args.resume_id,
        config_dict=vars(args),
    )

    def save_best_fn(policy: BasePolicy) -> None:
        torch.save(policy, os.path.join(log_path, "policy.pth"))
        # torch.save(policy.state_dict(), os.path.join(log_path, "policy.pth"))

    def save_checkpoint_fn(epoch: int, env_step: int, gradient_step: int) -> str:
        if (epoch%500) == 0:
            torch.save(policy, os.path.join(log_path, f"checkpoint_{epoch}.pth"))

    if not args.watch:
        # trainer
        result = OffpolicyTrainer(
            policy=policy,
            train_collector=train_collector,
            test_collector=test_collector,
            max_epoch=args.epoch,
            step_per_epoch=args.step_per_epoch,
            step_per_collect=args.step_per_collect,
            episode_per_test=args.test_num,
            batch_size=args.batch_size,
            save_best_fn=save_best_fn,
            save_checkpoint_fn=save_checkpoint_fn,
            logger=logger,
            update_per_step=args.update_per_step,
            test_in_train=False,
        ).run()
        pprint.pprint(result)

    # Let's watch its performance!
    policy.eval()
    test_envs.seed(args.seed)
    test_collector.reset()
    collector_stats = test_collector.collect(n_episode=args.test_num, render=args.render)
    print(collector_stats)


if __name__ == "__main__":
    test_td3()