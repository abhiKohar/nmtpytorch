# -*- coding: utf-8 -*-
import math
import time
from pathlib import Path

from .utils.misc import load_pt_file
from .utils.filterchain import FilterChain

from . import logger
from . import models
from .config import Options
from .search import beam_search


class Translator(object):
    """A utility class to pack translation related features."""

    def __init__(self, **kwargs):
        # Setup logger
        self.logger = logger.setup(None, 'translate')

        # Store attributes directly. See bin/nmtpy for their list.
        self.__dict__.update(kwargs)

        # How many models?
        self.n_models = len(self.models)

        # Print some information
        self.logger.info(
            '{} model(s) - beam_size: {}, batch_size: {}, max_len: {}'.format(
                self.n_models, self.beam_size, self.batch_size, self.max_len))

        # Store each model instance
        self.instances = []

        # Create model instances and move them to GPU
        for model_file in self.models:
            weights, _, opts = load_pt_file(model_file)
            opts = Options.from_dict(opts)
            # Create model instance
            instance = getattr(models, opts.train['model_type'])(
                opts=opts, logger=self.logger)
            # Setup layers
            instance.setup(reset_params=False)
            # Load weights
            instance.load_state_dict(weights, strict=True)
            # Move to GPU
            instance.cuda()
            # Switch to eval mode
            instance.train(False)
            self.instances.append(instance)

        # Do some sanity-check for ensembling compatibility
        self.sanity_check(self.instances)

        # Setup post-processing filters
        eval_filters = self.instances[0].opts.train['eval_filters']
        src_lang = self.instances[0].sl

        if self.disable_filters or not eval_filters:
            self.logger.info('Post-processing filters disabled.')
            self.filter = lambda s: s
        else:
            self.logger.info('Post-processing filters enabled.')
            self.filter = FilterChain(eval_filters)

        # Can be a comma separated list of hardcoded test splits
        if self.splits:
            self.logger.info('Will translate "{}"'.format(self.splits))
            self.splits = self.splits.split(',')
        elif self.source:
            # Split into key:value's and parse into dict
            input_dict = {}
            self.logger.info('Will translate input configuration:')
            for data_source in self.source.split(','):
                key, path = data_source.split(':', 1)
                input_dict[key] = Path(path)
                self.logger.info(' {}: {}'.format(key, input_dict[key]))
            self.instances[0].opts.data['new_set'] = input_dict
            self.splits = ['new']

    @staticmethod
    def sanity_check(instances):
        eval_filters = set([i.opts.train['eval_filters'] for i in instances])
        assert len(eval_filters) < 2, "eval_filters differ between instances."

        n_trg_vocab = set([i.n_trg_vocab for i in instances])
        assert len(n_trg_vocab) == 1, "target vocabularies differ."

    def translate(self, instances, split):
        """Returns the hypotheses generated by translating the given split
        using the given model instance.

        Arguments:
            instance(nn.Module): An initialized nmtpytorch model instance.
            split(str): A test split defined in the .conf file before
                training.

        Returns:
            list:
                A list of optionally post-processed string hypotheses.
        """

        # FIXME: Fetch first one for now
        instance = self.instances[0]

        instance.load_data(split)
        loader = instance.datasets[split].get_iterator(
            self.batch_size, only_source=True)

        self.logger.info('Starting translation')
        start = time.time()
        hyps = beam_search(instance, loader, instance.trg_vocab,
                           beam_size=self.beam_size, max_len=self.max_len,
                           avoid_double=self.avoid_double,
                           avoid_unk=self.avoid_unk)
        up_time = time.time() - start
        self.logger.info('Took {:.3f} seconds, {} sent/sec'.format(
            up_time, math.floor(len(hyps) / up_time)))

        return self.filter(hyps)

    def dump(self, hyps, split):
        """Writes the results into output.

        Arguments:
            hyps(list): A list of hypotheses.
        """
        suffix = ".beam{}".format(self.beam_size)
        if self.avoid_double:
            suffix += ".nodbl"
        if self.avoid_unk:
            suffix += ".nounk"

        if split == 'new':
            output = "{}{}".format(self.output, suffix)
        else:
            output = "{}.{}{}".format(self.output, split, suffix)

        with open(output, 'w') as f:
            for line in hyps:
                f.write(line + '\n')

    def __call__(self):
        """Dumps the hypotheses for each of the requested split/file."""
        for input_ in self.splits:
            # input_ can be a valid split name or 'new' when -S is given
            hyps = self.translate(self.instances, input_)
            self.dump(hyps, input_)
