import numpy as np
import os
import tensorflow as tf
from data_utils import minibatches, pad_sequences, ctc_label
from general_utils import Progbar, print_sentence, get_logger
from tensorflow.contrib.rnn import LSTMCell, MultiRNNCell, DropoutWrapper, LSTMStateTuple


class PhoneModel(object):
    def __init__(self, config, nb_char, id_to_char, logger=None):
        """
        Args:
            config: class with hyper parameters
            nb_char: the number of characters
            id_to_char: map char idx > char
            logger: logger instance
        """
        self.config     = config

        if logger is None:
            logger = get_logger('logger')

        self.logger = logger
        self.nb_char = nb_char
        self.id_to_char = id_to_char


    def add_placeholders(self):
        """
        Adds placeholders to self
        """

        # shape = (batch size, max length of sequence in batch, features)
        self.frames = tf.placeholder(tf.float32, shape=[None, None, 123],
                        name="feats")

        # shape = (batch size)
        self.sequence_lengths = tf.placeholder(tf.int32, shape=[None],
                        name="sequence_lengths")

        # shape = (batch size, max length of sequence in batch)
        # shape = (batch size) for ctc
        self.labels = tf.sparse_placeholder(tf.int32, name='labels')

        # hyper parameters
        self.keep_prob = tf.placeholder(dtype=tf.float32, shape=[],
                        name="dropout")
        self.lr = tf.placeholder(dtype=tf.float32, shape=[],
                        name="lr")


    def get_feed_dict(self, framelist, phones=None, lr=None, dropout=None):
        """
        Given some data, pad it and build a feed dictionary
        Args:
            framelist: list of frames. A frame is a list of 123 features.
            phones: list of ids
            lr: (float) learning rate
            dropout: (float) keep prob
        Returns:
            dict {placeholder: value}
        """
        frames, sequence_lengths = pad_sequences(framelist, np.zeros(123, dtype=np.float32))

        # build feed dictionary
        feed = {
            self.frames: frames,
            self.sequence_lengths: sequence_lengths
        }

        if phones is not None:
            # ctc_phones = ctc_label(phones)
            # padded_ctc_phones = pad_sequences(ctc_phones, 0)
            feed[self.labels] = self.ctc_sparse_label(phones)

        if lr is not None:
            feed[self.lr] = lr

        if dropout is not None:
            feed[self.keep_prob] = dropout

        return feed, sequence_lengths

    def add_logits_op(self, is_train = True):
        """
        Adds logits to self
        """
        with tf.variable_scope("bi-lstm"):
            if self.config.nb_layers == 1:
                self.lstm_cell_fwd = LSTMCell(self.config.hidden_size)
                self.lstm_cell_bwd = LSTMCell(self.config.hidden_size)
                self.lstm_cell_fwd_dropout = DropoutWrapper(self.lstm_cell_fwd,
                                                            output_keep_prob=self.keep_prob,
                                                            variational_recurrent=self.config.variational,
                                                            input_size=123,
                                                            dtype=tf.float32)
                self.lstm_cell_bwd_dropout = DropoutWrapper(self.lstm_cell_bwd,
                                                            output_keep_prob=self.keep_prob,
                                                            variational_recurrent=self.config.variational,
                                                            input_size=123,
                                                            dtype=tf.float32)
            else:
                # Multi-layers
                fwd_cells, bwd_cells, fwd_dropout_cells, bwd_dropout_cells = [], [], [], []
                for _ in range(self.config.nb_layers):
                    fwd_cell = LSTMCell(self.config.hidden_size)
                    bwd_cell = LSTMCell(self.config.hidden_size)
                    fwd_cells += [fwd_cell]
                    bwd_cells += [bwd_cell]

                    fwd_dropout_cell = DropoutWrapper(fwd_cell,
                                                      output_keep_prob=self.keep_prob,
                                                      variational_recurrent=self.config.variational,
                                                      input_size=123,
                                                      dtype=tf.float32)
                    bwd_dropout_cell = DropoutWrapper(bwd_cell,
                                                      output_keep_prob=self.keep_prob,
                                                      variational_recurrent=self.config.variational,
                                                      input_size=123,
                                                      dtype=tf.float32)
                    fwd_dropout_cells += [fwd_dropout_cell]
                    bwd_dropout_cells += [bwd_dropout_cell]

                self.lstm_cell_fwd = MultiRNNCell([fwd_cells])
                self.lstm_cell_bwd = MultiRNNCell([bwd_cells])
                self.lstm_cell_fwd_dropout = MultiRNNCell(fwd_dropout_cells)
                self.lstm_cell_bwd_dropout = MultiRNNCell(bwd_dropout_cells)

        if is_train:
            (output_fw, output_bw), _ = tf.nn.bidirectional_dynamic_rnn(self.lstm_cell_fwd_dropout,
                                                                        self.lstm_cell_bwd_dropout, self.frames,
                                                                        sequence_length=self.sequence_lengths,
                                                                        dtype=tf.float32)
        else:
            (output_fw, output_bw), _ = tf.nn.bidirectional_dynamic_rnn(self.lstm_cell_fwd,
                                                                        self.lstm_cell_bwd, self.frames,
                                                                        sequence_length=self.sequence_lengths,
                                                                        dtype=tf.float32)
        output = tf.concat([output_fw, output_bw], axis=-1)
        # output = tf.nn.dropout(output, self.config.dropout)
        with tf.variable_scope("proj"):
            W = tf.get_variable("W", shape=[2 * self.config.hidden_size, self.nb_char],
                                dtype=tf.float32)

            b = tf.get_variable("b", shape=[self.nb_char], dtype=tf.float32,
                                initializer=tf.zeros_initializer())

        ntime_steps = tf.shape(output)[1]
        output = tf.reshape(output, [-1, 2*self.config.hidden_size])
        pred = tf.matmul(output, W) + b
        self.logits = tf.reshape(pred, [-1, ntime_steps, self.nb_char])

    def add_pred_op(self):
        """
        Adds phones_pred to self
        """
        self.labels_pred = tf.cast(tf.argmax(self.logits, axis=-1), tf.int32)

    def add_ctc_pred_op(self):
        decoded, log_prob = tf.nn.ctc_greedy_decoder(logits, seq_len)

        # Accuracy: label error rate
        acc = tf.reduce_mean(tf.edit_distance(tf.cast(decoded[0], tf.int32),
                                      targets))

    def add_loss_op(self):
        """
        Adds loss to self
        """
        losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=self.logits, labels=self.labels)
        mask = tf.sequence_mask(self.sequence_lengths)
        losses = tf.boolean_mask(losses, mask)
        self.loss = tf.reduce_mean(losses)

        # for tensorboard
        tf.summary.scalar("loss", self.loss)

    @staticmethod
    def ctc_sparse_label(labels):
        # idx = tf.where(tf.not_equal(labels, 0))
        # sparse = tf.SparseTensor(idx, tf.gather_nd(dense_t, idx), dense_t.get_shape())
        # sparse = tf.SparseTensor(idx, tf.gather_nd(labels, idx), tf.shape(labels, out_type=tf.int64))
        indices, values = [], []
        max_length = 0
        for i, label in enumerate(labels):
            if len(label) > max_length: max_length = len(label)
            for j, char in enumerate(label):
                indices.append([i, j])
                values.append(char)
        shape = [len(labels), max_length]
        indices = np.asarray(indices, dtype=np.int64)
        values =  np.asarray(values, dtype=np.float32)
        shape = np.asarray(shape, dtype=np.int64)
        sparse = tf.SparseTensorValue(indices, values, shape)

        return sparse

    def add_ctc_loss(self):
        """
        Adds ctc_loss 
        """
        # sparse_labels = self.ctc_sparse_label(self.labels)
        ctc_loss = tf.nn.ctc_loss(self.labels, self.logits, self.sequence_lengths,
                                              preprocess_collapse_repeated=False, ctc_merge_repeated=True,
                                              time_major=False)
        self.loss = tf.reduce_mean(ctc_loss)
    def add_train_op(self):
        """
        Add train_op to self
        """

        with tf.variable_scope("train_step"):
            optimizer = tf.train.AdamOptimizer(self.lr)
            self.train_op = optimizer.minimize(self.loss)


    def add_init_op(self):
        self.init = tf.global_variables_initializer()


    def add_summary(self, sess):
        # tensorboard stuff
        self.merged = tf.summary.merge_all()
        self.file_writer = tf.summary.FileWriter(self.config.output_path, sess.graph)


    def build(self):
        self.add_placeholders()
        self.add_logits_op()
        self.add_pred_op()
        # self.add_loss_op()
        self.add_ctc_loss()
        self.add_train_op()
        self.add_init_op()


    def predict_batch(self, sess, framelist):
        """
        Args:
            sess: a tensorflow session
            framelist: list of frames
        Returns:
            phones_pred: list of phones for each sentence
            sequence_length
        """
        fd, sequence_lengths = self.get_feed_dict(framelist, dropout=1.0)

        phones_pred = sess.run(self.labels_pred, feed_dict=fd)

        return phones_pred, sequence_lengths


    def run_epoch(self, sess, train, dev, epoch):
        """
        Performs one complete pass over the train set and evaluate on dev
        Args:
            sess: tensorflow session
            train: dataset that yields tuple of sentences, tags
            dev: dataset
            epoch: (int) number of the epoch
        """
        nbatches = (len(train) + self.config.batch_size - 1) / self.config.batch_size
        prog = Progbar(target=nbatches, verbose=True)
        for i, (framelist, phones) in enumerate(minibatches(train, self.config.batch_size)):

            fd, _ = self.get_feed_dict(framelist, phones, self.config.lr, self.config.keep_prob)

            # _, train_loss, summary = sess.run([self.train_op, self.loss, self.merged], feed_dict=fd)
            _, train_loss = sess.run([self.train_op, self.loss], feed_dict=fd)
            prog.update(i + 1, [("train loss", train_loss)])

            # tensorboard
            # if i % 10 == 0:
            #     self.file_writer.add_summary(summary, epoch*nbatches + i)

        acc, per = self.run_evaluate(sess, dev)
        self.logger.info(" - dev accuracy {:04.2f} - PER {:04.2f}".format(100*acc, 100*per))
        return acc, per


    def run_evaluate(self, sess, test):
        """
        Evaluates performance on test set
        Args:
            sess: tensorflow session
            test: dataset that yields tuple of sentences, tags
        Returns:
            accuracy
            phone error rate
        """
        accs = []
        group_accuracy = []
        for framelist, phones in minibatches(test, self.config.batch_size):
            phones_pred, sequence_lengths = self.predict_batch(sess, framelist)

            for lab, lab_pred, length in zip(phones, phones_pred, sequence_lengths):
                lab = lab[:length]
                lab_pred = lab_pred[:length]

                accs += map(lambda x: x[0] == x[1], zip(lab, lab_pred))

                group = [self.phn2group[self.idx2phn[x]] for x in lab]
                group_pred = [self.phn2group[self.idx2phn[x]] for x in lab_pred]

                group_accuracy += map(lambda x: x[0] == x[1], zip(group, group_pred))

        acc = np.mean(accs)
        per = 1-np.mean(group_accuracy)
        return acc, per


    def train(self, train, dev):
        """
        Performs training with early stopping and lr exponential decay
        Args:
            train: dataset that yields tuple of frame, phoneindex
            dev: dataset
        """
        best_score = 2
        saver = tf.train.Saver()
        # for early stopping
        nepoch_no_imprv = 0
        # sv = tf.train.Supervisor(logdir=self.config.output_path)
        # with sv.managed_session() as sess:
        with tf.Session() as sess:
            sess.run(self.init)
            # tensorboard
            self.add_summary(sess)
            for epoch in range(self.config.nepochs):
                self.logger.info("Epoch {:} out of {:}".format(epoch + 1, self.config.nepochs))

                acc, per = self.run_epoch(sess, train, dev, epoch)

                # decay learning rate
                lr_decay = self.config.lr_decay ** max(epoch + 1 - 20, 0.0)
                self.config.lr *= lr_decay

                # early stopping and saving best parameters
                if per < best_score:
                    nepoch_no_imprv = 0
                    if not os.path.exists(self.config.model_output):
                        os.makedirs(self.config.model_output)
                    saver.save(sess, self.config.model_output)
                    best_score = per
                    self.logger.info("- new best score!")

                else:
                    nepoch_no_imprv += 1
                    if nepoch_no_imprv >= self.config.nepoch_no_imprv:
                        self.logger.info("- early stopping {} epochs without improvement".format(
                                        nepoch_no_imprv))
                        break

                # if FLAGS.save_path:
                #     print("Saving model to %s." % FLAGS.save_path)
                #     sv.saver.save(session, FLAGS.save_path, global_step=sv.global_step)


    def evaluate(self, test):
        saver = tf.train.Saver()
        with tf.Session() as sess:
            self.logger.info("Testing model over test set")
            saver.restore(sess, self.config.model_output)
            acc, per = self.run_evaluate(sess, test)
            self.logger.info(" - dev accuracy {:04.2f} - PER {:04.2f}".format(100*acc, 100*per))

