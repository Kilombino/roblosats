import React from 'react';
import { SvgIcon, type SvgIconProps } from '@mui/material';
import { ROBLOSATS_ROBOT } from './RoblosatsRobot';

// Roblosats: the robot and the name, instead of RoboSats' wordmark. The text takes the icon's
// fill (callers pass a gradient), as the old wordmark's paths did.
const RoboSatsText: React.FC<SvgIconProps> = (props) => {
  return (
    <SvgIcon {...props} viewBox='0 0 2000 700'>
      <image href={ROBLOSATS_ROBOT} x='0' y='50' width='600' height='600' />
      <text
        x='640'
        y='470'
        fontSize='300'
        fontWeight='700'
        fontFamily='Roboto, Helvetica, Arial, sans-serif'
        letterSpacing='-6'
      >
        Roblosats
      </text>
    </SvgIcon>
  );
};

export default RoboSatsText;
