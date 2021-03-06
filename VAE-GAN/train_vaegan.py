import argparse
import os, sys
import numpy as np
import datetime
import time
import chainer
from chainer import cuda
from chainer import serializers
import chainer.functions as F

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vaegan
from dataset import CelebA

try:
    import tensorflow as tf

    use_tensorboard = True
except:
    print('tensorflow is not installed')
    use_tensorboard = False


def progress_report(count, start_time, batchsize, emd):
    duration = time.time() - start_time
    throughput = count * batchsize / duration
    sys.stderr.write(
        '\r{} updates ({} samples) time: {} ({:.2f} samples/sec) emd : {:.5f}'.format(
            count, count * batchsize, str(datetime.timedelta(seconds=duration)).split('.')[0], throughput, emd
        )
    )
    sys.stdout.flush()


def visualize(gen, enc, train_iter, epoch, savedir, batch_size=64, image_type='sigmoid'):
    # save original image
    batch = train_iter.next()
    x = chainer.Variable(gen.xp.asarray([b[0] for b in batch[:batch_size // 2]], 'float32'), volatile=True)
    if image_type == 'sigmoid':
        img_origin = ((cuda.to_cpu(x.data)) * 255).clip(0, 255).astype(np.uint8)
    else:
        img_origin = ((cuda.to_cpu(x.data) + 1) * 127.5).clip(0, 255).astype(np.uint8)

    # save reconstruction image
    mu, var = enc(x, train=False)
    # z = F.gaussian(mu, var)
    x_rec = gen(mu, train=False)
    if image_type == 'sigmoid':
        img_rec = ((cuda.to_cpu(x_rec.data)) * 255).clip(0, 255).astype(np.uint8)
    else:
        img_rec = ((cuda.to_cpu(x_rec.data) + 1) * 127.5).clip(0, 255).astype(np.uint8)
    fig = plt.figure(figsize=(9, 9))
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1, hspace=0.05, wspace=0.05)
    for m in range(32):
        i = m / 8
        j = m % 8
        ax = fig.add_subplot(8, 8, 16 * i + j + 1, xticks=[], yticks=[])
        ax.imshow(img_origin[m].transpose(1, 2, 0))
        ax = fig.add_subplot(8, 8, 16 * i + j + 8 + 1, xticks=[], yticks=[])
        ax.imshow(img_rec[m].transpose(1, 2, 0))
    plt.savefig('{}/reconstruction_{:03d}'.format(savedir, epoch))
    plt.close()

    z = chainer.Variable(gen.xp.asarray(gen.make_hidden_normal(batch_size)), volatile=True)
    x_fake = gen(z, train=False)
    if image_type == 'sigmoid':
        img_gen = ((cuda.to_cpu(x_fake.data)) * 255).clip(0, 255).astype(np.uint8)
    else:
        img_gen = ((cuda.to_cpu(x_fake.data) + 1) * 127.5).clip(0, 255).astype(np.uint8)

    fig = plt.figure(figsize=(12, 12))
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1, hspace=0.05, wspace=0.05)
    for i in range(64):
        ax = fig.add_subplot(8, 8, i + 1, xticks=[], yticks=[])
        ax.imshow(img_gen[i].transpose(1, 2, 0))
    fig.savefig('{}/generate_{:03d}'.format(savedir, epoch))
    # plt.show()
    plt.close()


def main():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--gpu', '-g', type=int, default=0, help='GPU device ID')
    parser.add_argument('--epoch', '-e', type=int, default=300, help='# of epoch')
    parser.add_argument('--batch_size', '-b', type=int, default=100,
                        help='learning minibatch size')
    parser.add_argument('--g_hidden', type=int, default=128)
    parser.add_argument('--g_arch', type=int, default=1)
    parser.add_argument('--g_activate', type=str, default='sigmoid')
    parser.add_argument('--g_channel', type=int, default=512)
    parser.add_argument('--C', type=float, default=1)
    parser.add_argument('--d_arch', type=int, default=1)
    parser.add_argument('--d_iters', type=int, default=5)
    parser.add_argument('--d_clip', type=float, default=0.01)
    parser.add_argument('--d_channel', type=int, default=512)
    parser.add_argument('--initial_iter', type=int, default=10)
    parser.add_argument('--resume', default='')
    parser.add_argument('--out', default='')
    # args = parser.parse_args()
    args, unknown = parser.parse_known_args()

    # log directory
    out = datetime.datetime.now().strftime('%m%d%H%M')
    if args.out:
        out = out + '_' + args.out
    out_dir = os.path.abspath(os.path.join(os.path.curdir, "runs", out))
    os.makedirs(os.path.join(out_dir, 'models'), exist_ok=True)
    os.makedirs(os.path.join(out_dir, 'visualize'), exist_ok=True)

    # hyper parameter
    with open(os.path.join(out_dir, 'setting.txt'), 'w') as f:
        for k, v in args._get_kwargs():
            print('{} = {}'.format(k, v))
            f.write('{} = {}\n'.format(k, v))

    # tensorboard
    if use_tensorboard:
        sess = tf.Session()
        sess.run(tf.initialize_all_variables())

        summary_dir = os.path.join(out_dir, "summaries")
        loss_ = tf.placeholder(tf.float32)
        gen_loss_summary = tf.scalar_summary('gen_loss', loss_)
        dis_loss_summary = tf.scalar_summary('dis_loss', loss_)
        enc_loss_summary = tf.scalar_summary('enc_loss', loss_)
        rec_loss_summary = tf.scalar_summary('rec_loss', loss_)
        summary_writer = tf.train.SummaryWriter(summary_dir, sess.graph)

    # load celebA
    dataset = CelebA(image_type=args.g_activate)
    train_iter = chainer.iterators.MultiprocessIterator(dataset, args.batch_size)

    gen = vaegan.Generator(n_hidden=args.g_hidden, activate=args.g_activate, ch=args.g_channel)
    dis = vaegan.Discriminator(ch=args.d_channel)
    enc = vaegan.Encoder(n_hidden=args.g_hidden)

    if args.gpu >= 0:
        cuda.get_device(args.gpu).use()
        enc.to_gpu()
        gen.to_gpu()
        dis.to_gpu()

    optimizer_enc = chainer.optimizers.Adam(alpha=0.0001, beta1=0.5)
    optimizer_gen = chainer.optimizers.Adam(alpha=0.0001, beta1=0.5)
    optimizer_dis = chainer.optimizers.Adam(alpha=0.0001, beta1=0.5)

    optimizer_enc.setup(enc)
    optimizer_gen.setup(gen)
    optimizer_dis.setup(dis)

    optimizer_enc.add_hook(chainer.optimizer.WeightDecay(0.00001))
    optimizer_gen.add_hook(chainer.optimizer.WeightDecay(0.00001))
    optimizer_dis.add_hook(chainer.optimizer.WeightDecay(0.00001))

    if args.init_epoch is not None:
        serializers.load_hdf5(args.input + '.enc.model', enc)
        serializers.load_hdf5(args.input + '.enc.state', optimizer_enc)
        serializers.load_hdf5(args.input + '.gen.model', gen)
        serializers.load_hdf5(args.input + '.gen.state', optimizer_gen)
        serializers.load_hdf5(args.input + '.dis.model', dis)
        serializers.load_hdf5(args.input + '.dis.state', optimizer_dis)

    start_time = time.time()
    train_count = 0
    C = 1
    for epoch in range(1, args.epoch + 1):
        print('Epoch {}'.format(epoch))

        sum_L_gen = []
        sum_L_dis = []
        sum_L_enc = []
        sum_L_rec = []

        loop = 1000
        for i in range(loop):
            batch = train_iter.next()
            x = chainer.Variable(gen.xp.asarray([b[0] for b in batch], 'float32'))
            # attr = chainer.Variable(gen.xp.asarray([b[1] for b in batch], 'int32'))

            # real image
            y_real, l2_real, l3_real = dis(x)
            L_dis_real = F.softmax_cross_entropy(y_real,
                                                 chainer.Variable(gen.xp.zeros(args.batch_size).astype(np.int32)))

            # fake image from random noize
            z = chainer.Variable(gen.xp.asarray(gen.make_hidden_normal(args.batch_size)))
            x_fake = gen(z)
            y_fake, _, _ = dis(x_fake)
            L_gen_fake = F.softmax_cross_entropy(y_fake,
                                                 chainer.Variable(gen.xp.zeros(args.batch_size).astype(np.int32)))
            L_dis_fake = F.softmax_cross_entropy(y_fake,
                                                 chainer.Variable(gen.xp.ones(args.batch_size).astype(np.int32)))

            # fake image from reconstruction
            mu_z, ln_var_z = enc(x)
            z_rec = F.gaussian(mu_z, ln_var_z)
            x_rec = gen(z_rec)
            y_rec, l2_rec, l3_rec = dis(x_rec)

            L_prior = F.gaussian_kl_divergence(mu_z, ln_var_z) / args.batch_size
            L_gen_rec = F.softmax_cross_entropy(y_rec, chainer.Variable(gen.xp.zeros(args.batch_size).astype(np.int32)))
            L_dis_rec = F.softmax_cross_entropy(y_rec, chainer.Variable(gen.xp.ones(args.batch_size).astype(np.int32)))

            L_rec = F.mean_squared_error(l2_real, l2_rec) * l2_real.data.shape[2] * l2_real.data.shape[3]
            # L_rec = F.gaussian_nll(l0, l2, chainer.Variable(xp.zeros(l0.data.shape).astype('float32'))) / args.batch_size

            # calc loss
            L_dis = L_dis_real + 0.5 * L_dis_fake + 0.5 * L_dis_rec
            L_enc = L_prior + L_rec
            L_gen = L_gen_fake + L_gen_rec + args.C * L_rec

            # update
            optimizer_enc.target.cleargrads()
            L_enc.backward()
            optimizer_enc.update()

            optimizer_gen.target.cleargrads()
            L_gen.backward()
            optimizer_gen.update()

            optimizer_dis.target.cleargrads()
            L_dis.backward()
            optimizer_dis.update()

            train_count += 1

            l_dis = float(L_dis.data)
            l_gen = float(L_gen.data)
            l_enc = float(L_enc.data)
            l_rec = float(L_rec.data)
            sum_L_dis.append(l_dis)
            sum_L_gen.append(l_gen)
            sum_L_enc.append(l_enc)
            sum_L_rec.append(l_rec)

            progress_report(train_count, start_time, args.batch_size, l_dis)

            if use_tensorboard:
                summary = sess.run(gen_loss_summary, feed_dict={loss_: l_gen})
                summary_writer.add_summary(summary, train_count)
                summary = sess.run(dis_loss_summary, feed_dict={loss_: l_dis})
                summary_writer.add_summary(summary, train_count)
                summary = sess.run(enc_loss_summary, feed_dict={loss_: l_enc})
                summary_writer.add_summary(summary, train_count)
                summary = sess.run(rec_loss_summary, feed_dict={loss_: l_rec})
                summary_writer.add_summary(summary, train_count)

        log = 'gen loss={:.5f}, dis loss={:.5f} enc loss={:.5f} rec loss={:.5f}' \
            .format(np.mean(sum_L_gen), np.mean(sum_L_dis), np.mean(sum_L_enc), np.mean(sum_L_rec))
        print('\n' + log)
        with open(os.path.join(out_dir, "log"), 'a+') as f:
            f.write(log + '\n')

        if epoch % 5 == 0:
            serializers.save_hdf5(os.path.join(out_dir, "models", "{:03d}.dis.model".format(epoch)), dis)
            serializers.save_hdf5(os.path.join(out_dir, "models", "{:03d}.gen.model".format(epoch)), gen)
            serializers.save_hdf5(os.path.join(out_dir, "models", "{:03d}.enc.model".format(epoch)), enc)

        visualize(gen, enc, train_iter, epoch=epoch, savedir=os.path.join(out_dir, 'visualize'),
                  image_type=args.g_activate)


if __name__ == '__main__':
    main()
