"""
Wrapper's for running optavc with a single method call.


run_optavc() currently supports:
    running finite differences of singlepoints to calculate Hessians
    Running optimizations by performing finite differences of gradients.

run_optavc_mpi()
    has never been used or tested all code is legacy for use on NERSC. Use WILL require work

"""

from optavc.options import Options
from . import optimize
from . import findifcalcs
from .template import TemplateFileProcessor


def run_optavc_mpi():
    from optavc.mpi4py_iface import mpirun
    options_kwargs = {
        'template_file_path': "template.dat",
        'queue': "shared",
        'command': "qchem input.dat output.dat",
        'time_limit': "48:00:00",  # has no effect
        # 'memory'            : "10 GB", #calculator uses memory, number of nodes, and numcores to
        # distribute resources
        'energy_regex': r"CCSD\(T\) total energy[=\s]+(-\d+\.\d+)",
        'success_regex': r"beer",
        'fail_regex': r"coffee",
        'input_name': "input.dat",
        'output_name': "output.dat",
        # 'readhess'          : False,
        'mpi': True,  # set to true to use mpi, false to not
        # 'submitter'         : None,
        'maxiter': 20,
        'findif': {
            'points': 3
        },  # set to 5 if you want slightly better frequencies
        # 'job_array'         : True,
        'optking': {
            'max_force_g_convergence': 1e-7,  # tighter than this is not recommended
            'rms_force_g_convergence': 1e-7,
        }
    }

    options_obj = Options(**options_kwargs)
    mpirun(options_obj)


def run_optavc(jobtype, options_dict, restart_iteration=0, xtpl_restart=None, sow=True, path="."):
    """ optavc driver meant to unify input. Create options object, needed template objects, start
    Hessian or Optimization calculation

    Parameters
    ----------
    jobtype: str
        generous with available strings for optimziation / hessian
    options_dict: dict
        should contain BOTH optavc and psi4 options
    restart_iteration: int
        corresponds to the iteration we should begin trying to sow gradients for
    xtpl_restart: str
        should be ['high_corr', 'large_basis', 'med_basis', 'small_basis']
    sow: bool
        if False optavc will try to reap only
    path: str
        for Hessian jobs only

    Returns
    -------
    True : if job exited successfully
    """

    options_obj = Options(**options_dict)

    # Make all required input_file objects required. xtpl job should only require 2

    if options_obj.xtpl:
        tfps = [TemplateFileProcessor(open(i).read(), options_obj) for i in
                options_obj.xtpl_templates]
        tfp = tfps[0]
        xtpl_inputs = [i.input_file_object for i in tfps]
    else:
        template_file_string = open(options_obj.template_file_path).read()
        tfp = TemplateFileProcessor(template_file_string, options_obj)
        xtpl_inputs = None

    input_obj = tfp.input_file_object
    molecule = tfp.molecule

    if jobtype.upper() in ['OPT', "OPTIMIZATION"]:
        opt_obj = optimize.Optimization(molecule, input_obj, options_obj, xtpl_inputs)
        return opt_obj.run(restart_iteration, xtpl_restart)
    elif jobtype.upper() in ["HESS", "FREQUENCY", "FREQUENCIES", "HESSIAN"]:
        if options_obj.xtpl:
            findifcalcs.Hessian.xtpl_hessian(molecule, xtpl_inputs, options_obj, path, sow)
        else:
            hess_obj = findifcalcs.Hessian(molecule, input_obj, options_obj, path=path)
            if sow:
                hess_obj.compute_result()
            else:
                hess_obj.reap()
            return True
    else:
        raise ValueError(
            """Can only run deriv or optimizations. For gradients see findifcalcs.py to run or
            add wrapper here""")
